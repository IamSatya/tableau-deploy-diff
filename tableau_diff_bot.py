#!/usr/bin/env python3
"""
Single-PR-comment Tableau diff bot with improved extraction/retries and support for arbitrarily large diffs.

Key additions:
- Delay + retry extraction for large files to avoid transient failures.
- If content/diff is too large for a comment, create a Gist and link to it.
- Optionally create one PR comment per file as well as the single aggregated comment.
"""
import os
import re
import time
import zipfile
import tempfile
import base64
import traceback
import logging
from pathlib import Path
from typing import Optional, List, Dict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import difflib
from dotenv import load_dotenv
import html
import json

load_dotenv()

# Required
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")
BOT_USERNAME = os.getenv("BOT_USERNAME", "tableau-diff-bot")

# Behavior config
MAX_LINES_PER_SECTION = int(os.getenv("MAX_LINES_PER_SECTION", "1000"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SEARCHABLE_PR_TAG = os.getenv("SEARCHABLE_PR_TAG", "#tableau-diff-pr")
# Extraction delay/retry config
EXTRACTION_DELAY_THRESHOLD_BYTES = int(os.getenv("EXTRACTION_DELAY_THRESHOLD_BYTES", str(8_000_000)))  # 8MB
EXTRACTION_INITIAL_DELAY_SEC = float(os.getenv("EXTRACTION_INITIAL_DELAY_SEC", "2"))
EXTRACTION_MAX_RETRIES = int(os.getenv("EXTRACTION_MAX_RETRIES", "4"))
EXTRACTION_BACKOFF_FACTOR = float(os.getenv("EXTRACTION_BACKOFF_FACTOR", "2"))
# Comment / gist thresholds
MAX_COMMENT_CHARS = int(os.getenv("MAX_COMMENT_CHARS", "60000"))
UPLOAD_TO_GIST_THRESHOLD_CHARS = int(os.getenv("UPLOAD_TO_GIST_THRESHOLD_CHARS", "50000"))
CREATE_PER_FILE_COMMENTS = os.getenv("CREATE_PER_FILE_COMMENTS", "false").lower() in ("1","true","yes")
GIST_PUBLIC = os.getenv("GIST_PUBLIC", "false").lower() in ("1","true","yes")

# logging
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tableau-diff-single-pr")

if not GITHUB_TOKEN:
    logger.error("GITHUB_TOKEN is not set. Exiting.")
    raise SystemExit(1)

# requests session with retries
session = requests.Session()
session.headers.update({
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": BOT_USERNAME,
})
retries = Retry(total=5, backoff_factor=1,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET", "POST", "PUT", "DELETE", "PATCH"))
session.mount("https://", HTTPAdapter(max_retries=retries))


def normalize_xml_for_diff(xml_text: str) -> str:
    if not xml_text:
        return ""
    lines = xml_text.splitlines()
    cleaned = []
    patterns_drop = [
        re.compile(r".*created-at=.*", re.IGNORECASE),
        re.compile(r".*creationtime=.*", re.IGNORECASE),
        re.compile(r".*last-modified.*", re.IGNORECASE),
        re.compile(r".*modified-time.*", re.IGNORECASE),
    ]
    patterns_mask = [
        (re.compile(r'(<workbook.*?project-luid=")[^"]+(")') , r"\1<redacted>\2"),
        (re.compile(r'(<datasource.*?luid=")[^"]+(")') , r"\1<redacted>\2"),
        (re.compile(r'(<connection.*?id=")[^"]+(")') , r"\1<redacted>\2"),
        (re.compile(r'(<uid>)[^<]+(</uid>)') , r"\1<redacted>\2"),
    ]
    for ln in lines:
        skip = False
        for p in patterns_drop:
            if p.match(ln.strip()):
                skip = True
                break
        if skip:
            continue
        new_ln = ln
        for p, repl in patterns_mask:
            new_ln = p.sub(repl, new_ln)
        cleaned.append(new_ln)
    return "\n".join(cleaned)


def _should_delay_before_extract(path: str) -> bool:
    try:
        size = Path(path).stat().st_size
        return size >= EXTRACTION_DELAY_THRESHOLD_BYTES
    except Exception:
        return False


def extract_twb_content_with_retries(path: str, original_name: str) -> str:
    """
    Attempt extraction with delay + retries for large files.
    """
    attempt = 0
    delay = EXTRACTION_INITIAL_DELAY_SEC if _should_delay_before_extract(path) else 0
    while attempt <= EXTRACTION_MAX_RETRIES:
        if delay > 0:
            logger.info(f"Delaying {delay}s before extraction attempt {attempt+1} for {original_name}")
            time.sleep(delay)
        try:
            content = _extract_twb_content(path, original_name)
            if content is not None and content != "":
                return content
            # if extraction returned empty, treat as transient and retry
            logger.warning(f"Extraction returned empty on attempt {attempt+1} for {original_name}")
        except Exception as e:
            logger.warning(f"Extraction attempt {attempt+1} failed for {original_name}: {e}")
        attempt += 1
        delay *= EXTRACTION_BACKOFF_FACTOR if delay > 0 else 1
    logger.error(f"All extraction attempts failed for {original_name}")
    return ""


def _extract_twb_content(path: str, original_name: str) -> str:
    """
    Core extraction logic (single attempt).
    Returns empty string on failure.
    """
    logger.info(f"[extract] Processing {original_name}; exists={Path(path).exists()}")
    try:
        if original_name.lower().endswith(".twb"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        elif original_name.lower().endswith(".twbx"):
            # Try zip
            if not zipfile.is_zipfile(path):
                logger.warning("Not a zipfile, trying fallback text read")
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                        if text.strip().startswith("<?xml"):
                            return text
                except Exception:
                    pass
                return ""
            with zipfile.ZipFile(path, "r") as z:
                files = z.namelist()
                twb_files = [f for f in files if f.lower().endswith(".twb")]
                if not twb_files:
                    xml_candidates = [f for f in files if f.lower().endswith(".xml")]
                    twb_files = xml_candidates
                if not twb_files:
                    return ""
                # choose largest
                best = None
                best_size = -1
                for f in twb_files:
                    info = z.getinfo(f)
                    if info.file_size > best_size:
                        best = f
                        best_size = info.file_size
                with z.open(best) as inner:
                    raw = inner.read()
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
    except Exception:
        logger.exception("Error in extraction")
    return ""


def generate_minimal_diff(old_content: str, new_content: str) -> List[str]:
    old_norm = normalize_xml_for_diff(old_content).splitlines()
    new_norm = normalize_xml_for_diff(new_content).splitlines()
    diff = difflib.unified_diff(old_norm, new_norm, fromfile="old.twb", tofile="new.twb", lineterm="")
    return [line for line in diff if line.startswith(("+", "-", "@@"))]


def create_gist(files: Dict[str, str], description: str = "Tableau diff", public: bool = GIST_PUBLIC) -> Optional[str]:
    """
    Create a gist with the provided files dict {filename: content}.
    Returns URL of the created gist or None on failure.
    """
    url = f"{GITHUB_API}/gists"
    payload = {"files": {name: {"content": content} for name, content in files.items()},
               "description": description,
               "public": public}
    try:
        r = session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code in (200, 201):
            data = r.json()
            gist_url = data.get("html_url")
            logger.info(f"Created gist: {gist_url}")
            return gist_url
        else:
            logger.warning(f"Failed to create gist: {r.status_code} {r.text}")
    except Exception:
        logger.exception("Exception creating gist")
    return None


def _fetch_file_from_contents_api(owner: str, repo: str, file_path: str, ref: str) -> bytes:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{file_path}?ref={ref}"
    r = session.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code == 200:
        data = r.json()
        if data.get("encoding") == "base64" and "content" in data:
            return base64.b64decode(data["content"])
        elif data.get("download_url"):
            r2 = session.get(data["download_url"], timeout=REQUEST_TIMEOUT)
            if r2.status_code == 200:
                return r2.content
            else:
                raise RuntimeError("download_url failed")
        else:
            raise RuntimeError("unexpected contents response")
    elif r.status_code == 404:
        raise FileNotFoundError(f"{file_path}@{ref} not found")
    else:
        r.raise_for_status()


def fetch_pr_files(owner: str, repo: str, pr_number: str) -> List[dict]:
    results = []
    page = 1
    per_page = 100
    while True:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files?page={page}&per_page={per_page}"
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.error(f"Failed to fetch PR files: {r.status_code} {r.text}")
            break
        chunk = r.json()
        if not isinstance(chunk, list):
            logger.error("Unexpected PR files response")
            break
        results.extend(chunk)
        if len(chunk) < per_page:
            break
        page += 1
    return results


def _find_existing_pr_comment(owner: str, repo: str, pr_number: str, pr_tag: str) -> Optional[dict]:
    page = 1
    per_page = 100
    while True:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments?page={page}&per_page={per_page}"
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"Could not list comments: {r.status_code}")
            return None
        comments = r.json()
        if not comments:
            return None
        for c in comments:
            user = c.get("user", {}).get("login", "")
            body = c.get("body", "") or ""
            if user.lower() == BOT_USERNAME.lower() and pr_tag in body:
                return c
        if len(comments) < per_page:
            break
        page += 1
    return None


def _create_pr_comment(owner: str, repo: str, pr_number: str, body: str) -> dict:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    r = session.post(url, json={"body": body}, timeout=REQUEST_TIMEOUT)
    if r.status_code in (200, 201):
        return r.json()
    else:
        logger.warning(f"Failed to create PR comment: {r.status_code} {r.text}")
        return {"error": r.status_code, "text": r.text}


def _update_pr_comment(owner: str, repo: str, comment_id: int, body: str) -> dict:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{comment_id}"
    r = session.patch(url, json={"body": body}, timeout=REQUEST_TIMEOUT)
    if r.status_code == 200:
        return r.json()
    else:
        logger.warning(f"Failed to update PR comment {comment_id}: {r.status_code} {r.text}")
        return {"error": r.status_code, "text": r.text}


def _create_or_update_file_comment(owner: str, repo: str, pr_number: str, file_path: str, body: str):
    """
    Create or update a dedicated comment for a file. This mirrors the earlier per-file behavior.
    """
    # find existing bot comment for this file by searching tag "#tableau-file <file_path>"
    tag = f"#tableau-file {file_path}"
    page = 1
    per_page = 100
    existing_id = None
    while True:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments?page={page}&per_page={per_page}"
        r = session.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"Could not list comments: {r.status_code}")
            break
        comments = r.json()
        if not comments:
            break
        for c in comments:
            user = c.get("user", {}).get("login", "")
            cb = c.get("body", "") or ""
            if user.lower() == BOT_USERNAME.lower() and tag in cb:
                existing_id = c.get("id")
                break
        if existing_id or len(comments) < per_page:
            break
        page += 1
    if existing_id:
        _update_pr_comment(owner, repo, existing_id, body)
    else:
        _create_pr_comment(owner, repo, pr_number, body)


def build_single_pr_comment(owner: str, repo: str, pr_number: str, file_summaries: List[Dict]) -> str:
    header = f"{SEARCHABLE_PR_TAG} {pr_number}\n\n"
    intro = (
        f"Automated Tableau diff summary for PR **{pr_number}**.\n\n"
        "This comment is managed by the bot and will be updated on subsequent runs.\n\n"
    )
    table_lines = ["| File | Status | Preview |", "|---|---:|---|"]
    for s in file_summaries:
        preview = (s.get("preview") or "").replace("\n", " ")[:200]
        table_lines.append(f"| `{html.escape(s['file_path'])}` | {s['status']} | {html.escape(preview)} |")

    body_parts = [header, intro, "\n".join(table_lines), "\n\n---\n"]

    for s in file_summaries:
        fp = html.escape(s["file_path"])
        status = s["status"]
        title = f"**{fp}** — {status}"
        body_parts.append(f"### {title}\n")
        preview = s.get("preview") or "(no preview available)"
        body_parts.append(f"**Preview:**\n\n{preview}\n\n")

        if status in ("added", "removed"):
            content = s.get("content") or ""
            # Decide to inline or gist
            if content and len(content) > UPLOAD_TO_GIST_THRESHOLD_CHARS:
                gist_url = create_gist({f"{s['file_path']}.xml": content}, description=f"{s['file_path']} content for PR {pr_number}")
                if gist_url:
                    body_parts.append(f"Full content is large — view it on a gist: {gist_url}\n\n")
                else:
                    body_parts.append("Full content is large but gist creation failed; showing first part below.\n\n")
                    # fallback to first section
                    lines = content.splitlines()
                    chunk = lines[:MAX_LINES_PER_SECTION]
                    body_parts.append("```xml\n" + "\n".join(chunk) + "\n```\n\n")
            else:
                lines = content.splitlines()
                if not lines:
                    body_parts.append("_(No content to show)_\n\n")
                else:
                    total = (len(lines) - 1) // MAX_LINES_PER_SECTION + 1
                    for i in range(0, len(lines), MAX_LINES_PER_SECTION):
                        chunk = lines[i: i + MAX_LINES_PER_SECTION]
                        part = i // MAX_LINES_PER_SECTION + 1
                        details = (
                            f"<details>\n<summary>Part {part}/{total} — click to expand</summary>\n\n"
                            f"```xml\n" + "\n".join(chunk) + "\n```\n\n</details>\n"
                        )
                        body_parts.append(details)
        elif status == "modified":
            diff_lines = s.get("diff_lines") or []
            if not diff_lines:
                body_parts.append("✅ No meaningful changes detected.\n\n")
            else:
                joined = "\n".join(diff_lines)
                if len(joined) > UPLOAD_TO_GIST_THRESHOLD_CHARS:
                    gist_url = create_gist({f"{s['file_path']}.diff": joined}, description=f"Diff for {s['file_path']} (PR {pr_number})")
                    if gist_url:
                        body_parts.append(f"Diff is large — view full diff in a gist: {gist_url}\n\n")
                    else:
                        body_parts.append("Diff is large but gist creation failed; showing first part below.\n\n")
                        # fall through to show first chunk
                # show chunks (either if small or fallback)
                total = (len(diff_lines) - 1) // MAX_LINES_PER_SECTION + 1
                for i in range(0, len(diff_lines), MAX_LINES_PER_SECTION):
                    chunk = diff_lines[i: i + MAX_LINES_PER_SECTION]
                    part = i // MAX_LINES_PER_SECTION + 1
                    details = (
                        f"<details>\n<summary>Diff Part {part}/{total} — click to expand</summary>\n\n"
                        f"```diff\n" + "\n".join(chunk) + "\n```\n\n</details>\n"
                    )
                    body_parts.append(details)
        else:
            body_parts.append("_(Unknown status)_\n\n")
        body_parts.append("\n---\n")

    body_parts.append("\n*Tip:* Search for the tag `" + SEARCHABLE_PR_TAG + f" {pr_number}` to quickly find this comment.")
    return "\n".join(body_parts)


def process_pull_request(owner: str, repo: str, pr_number: str, base_branch: str, head_branch: str):
    try:
        files = fetch_pr_files(owner, repo, pr_number)
        if not files:
            logger.info("No files in PR")
            return

        file_summaries = []
        for change in files:
            file_path = change.get("filename")
            status = change.get("status")
            if not file_path or not status:
                continue
            if not (file_path.lower().endswith(".twb") or file_path.lower().endswith(".twbx")):
                continue

            old_xml = ""
            new_xml = ""
            with tempfile.TemporaryDirectory() as tmpdir:
                old_path = os.path.join(tmpdir, "old_file")
                new_path = os.path.join(tmpdir, "new_file")

                if status != "added":
                    try:
                        content_bytes = _fetch_file_from_contents_api(owner, repo, file_path, base_branch)
                        with open(old_path, "wb") as f:
                            f.write(content_bytes)
                        old_xml = extract_twb_content_with_retries(old_path, file_path)
                    except FileNotFoundError:
                        logger.warning(f"Old file not found: {file_path}@{base_branch}")
                    except Exception:
                        logger.exception("Failed to fetch old file")

                if status != "removed":
                    try:
                        content_bytes = _fetch_file_from_contents_api(owner, repo, file_path, head_branch)
                        with open(new_path, "wb") as f:
                            f.write(content_bytes)
                        new_xml = extract_twb_content_with_retries(new_path, file_path)
                    except FileNotFoundError:
                        logger.warning(f"New file not found: {file_path}@{head_branch}")
                    except Exception:
                        logger.exception("Failed to fetch new file")

            summary = {"file_path": file_path, "status": status}
            preview_source = new_xml or old_xml or ""
            preview_lines = []
            for ln in preview_source.splitlines():
                if ln.strip():
                    preview_lines.append(ln.strip())
                if len(preview_lines) >= 6:
                    break
            summary["preview"] = "\n".join(preview_lines)

            if status == "added" and new_xml:
                summary["content"] = new_xml
            elif status == "removed" and old_xml:
                summary["content"] = old_xml
            elif old_xml and new_xml:
                summary["diff_lines"] = generate_minimal_diff(old_xml, new_xml)
            else:
                summary["preview"] = summary["preview"] or "(could not extract content)"
            file_summaries.append(summary)

        # Build and create/update single PR comment
        body = build_single_pr_comment(owner, repo, pr_number, file_summaries)
        pr_tag = f"{SEARCHABLE_PR_TAG} {pr_number}"
        existing = _find_existing_pr_comment(owner, repo, pr_number, pr_tag)
        if existing and existing.get("id"):
            cid = existing["id"]
            _update_pr_comment(owner, repo, cid, body)
            logger.info(f"Updated single PR comment id={cid}")
        else:
            _create_pr_comment(owner, repo, pr_number, body)
            logger.info("Created single PR comment")

        # Optionally create/update per-file comments
        if CREATE_PER_FILE_COMMENTS:
            for s in file_summaries:
                file_tag = f"#tableau-file {s['file_path']}"
                # For each file create a concise body: include tag, preview and link to gist if created earlier.
                fb_parts = [file_tag + "\n\n", f"**File:** `{html.escape(s['file_path'])}`\n\n", f"**Status:** {s['status']}\n\n", f"**Preview:**\n\n{s.get('preview') or '(no preview)'}\n\n"]
                # Attempt to include small inline snippet if not too large, else create gist and link
                inline_text = ""
                if s.get("status") in ("added","removed"):
                    content = s.get("content") or ""
                    if len(content) > UPLOAD_TO_GIST_THRESHOLD_CHARS:
                        gist_url = create_gist({f"{s['file_path']}.xml": content}, description=f"{s['file_path']} content (PR {pr_number})")
                        if gist_url:
                            fb_parts.append(f"Full content: {gist_url}\n\n")
                    else:
                        inline_text = "\n".join((content.splitlines()[:MAX_LINES_PER_SECTION]))
                else:
                    diff_lines = s.get("diff_lines") or []
                    joined = "\n".join(diff_lines)
                    if len(joined) > UPLOAD_TO_GIST_THRESHOLD_CHARS:
                        gist_url = create_gist({f"{s['file_path']}.diff": joined}, description=f"Diff for {s['file_path']} (PR {pr_number})")
                        if gist_url:
                            fb_parts.append(f"Full diff: {gist_url}\n\n")
                    else:
                        inline_text = "\n".join(diff_lines[:MAX_LINES_PER_SECTION])
                if inline_text:
                    fb_parts.append("```diff\n" + inline_text + "\n```\n")
                body_file = "\n".join(fb_parts)
                _create_or_update_file_comment(owner, repo, pr_number, s['file_path'], body_file)

    except Exception:
        logger.exception("Error in process_pull_request")


def main():
    owner = os.getenv("OWNER")
    repo = os.getenv("REPO")
    pr_number = os.getenv("PR_NUMBER")
    head_branch = os.getenv("HEAD_BRANCH")
    base_branch = os.getenv("BASE_BRANCH")

    logger.info(f"Starting single-PR-comment bot for {owner}/{repo} PR {pr_number}")

    if not all([owner, repo, pr_number, head_branch, base_branch]):
        logger.error("Missing required environment variables: OWNER, REPO, PR_NUMBER, HEAD_BRANCH, BASE_BRANCH")
        return

    process_pull_request(owner, repo, pr_number, base_branch, head_branch)


if __name__ == "__main__":
    main()
