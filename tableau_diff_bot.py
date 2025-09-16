#!/usr/bin/env python3
"""
Tableau diff bot — patched, full script.

Features:
- Extracts .twb/.twbx from PR changes, normalizes XML, computes minimal diffs.
- Builds aggregated PR content divided into file sections.
- Packs sections into multiple PR comments of <= MAX_COMMENT_CHARS characters.
- If a single file-section itself is too big, attempts to create a Gist and post a small link.
- If gist creation fails or creating comment parts returns 422, falls back to per-file comments + tiny summary.
- Cleans up previous bot-managed aggregated comments before posting updated comments.
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

# Required / defaults
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
MAX_COMMENT_CHARS = int(os.getenv("MAX_COMMENT_CHARS", "60000"))  # must be <= GitHub limit (65536)
UPLOAD_TO_GIST_THRESHOLD_CHARS = int(os.getenv("UPLOAD_TO_GIST_THRESHOLD_CHARS", "50000"))
CREATE_PER_FILE_COMMENTS = os.getenv("CREATE_PER_FILE_COMMENTS", "false").lower() in ("1","true","yes")
GIST_PUBLIC = os.getenv("GIST_PUBLIC", "false").lower() in ("1","true","yes")

# logging
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tableau-diff-splitter")

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


# ---------- Extraction and normalization ----------
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
    attempt = 0
    delay = EXTRACTION_INITIAL_DELAY_SEC if _should_delay_before_extract(path) else 0
    while attempt <= EXTRACTION_MAX_RETRIES:
        if delay > 0:
            logger.info(f"Delaying {delay}s before extraction attempt {attempt+1} for {original_name}")
            time.sleep(delay)
        try:
            content = _extract_twb_content(path, original_name)
            if content:
                return content
            logger.warning(f"Extraction returned empty on attempt {attempt+1} for {original_name}")
        except Exception as e:
            logger.warning(f"Extraction attempt {attempt+1} failed for {original_name}: {e}")
        attempt += 1
        delay = delay * EXTRACTION_BACKOFF_FACTOR if delay > 0 else 0
    logger.error(f"All extraction attempts failed for {original_name}")
    return ""


def _extract_twb_content(path: str, original_name: str) -> str:
    logger.info(f"[extract] Processing {original_name}; exists={Path(path).exists()}")
    try:
        if original_name.lower().endswith(".twb"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        elif original_name.lower().endswith(".twbx"):
            if not zipfile.is_zipfile(path):
                logger.warning("[extract] Not a zipfile; trying fallback text read")
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


# ---------- Diff generation ----------
def generate_minimal_diff(old_content: str, new_content: str) -> List[str]:
    old_norm = normalize_xml_for_diff(old_content).splitlines()
    new_norm = normalize_xml_for_diff(new_content).splitlines()
    diff = difflib.unified_diff(old_norm, new_norm, fromfile="old.twb", tofile="new.twb", lineterm="")
    return [line for line in diff if line.startswith(("+", "-", "@@"))]


# ---------- GitHub helpers (gist, comments) ----------
def create_gist(files: Dict[str, str], description: str = "Tableau diff", public: bool = GIST_PUBLIC) -> Optional[str]:
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
        elif r.status_code == 404:
            logger.warning("Gist creation returned 404 — token may lack 'gist' scope or endpoint disabled in environment.")
            return None
        else:
            logger.warning(f"Failed to create gist: {r.status_code} {r.text}")
            return None
    except Exception:
        logger.exception("Exception creating gist")
    return None


def _create_pr_comment(owner: str, repo: str, pr_number: str, body: str) -> dict:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    r = session.post(url, json={"body": body}, timeout=REQUEST_TIMEOUT)
    if r.status_code in (200, 201):
        return r.json()
    else:
        if r.status_code == 422 and "Body is too long" in (r.text or ""):
            logger.warning("PR comment too long (422).")
            return {"error": 422, "reason": "too_long", "text": r.text}
        logger.warning(f"Failed to create PR comment: {r.status_code} {r.text}")
        return {"error": r.status_code, "text": r.text}


def _update_pr_comment(owner: str, repo: str, comment_id: int, body: str) -> dict:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{comment_id}"
    r = session.patch(url, json={"body": body}, timeout=REQUEST_TIMEOUT)
    if r.status_code == 200:
        return r.json()
    else:
        if r.status_code == 422 and "Body is too long" in (r.text or ""):
            logger.warning("PR comment update rejected: body too long (422).")
            return {"error": 422, "reason": "too_long", "text": r.text}
        logger.warning(f"Failed to update PR comment {comment_id}: {r.status_code} {r.text}")
        return {"error": r.status_code, "text": r.text}


def _list_bot_pr_comments(owner: str, repo: str, pr_number: str, pr_tag: str) -> List[dict]:
    found = []
    page = 1
    per_page = 100
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
            body = c.get("body", "") or ""
            if user.lower() == BOT_USERNAME.lower() and pr_tag in body:
                found.append(c)
        if len(comments) < per_page:
            break
        page += 1
    return found


def _delete_comment(owner: str, repo: str, comment_id: int) -> bool:
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{comment_id}"
    r = session.delete(url, timeout=REQUEST_TIMEOUT)
    if r.status_code in (204,):
        logger.info(f"Deleted old bot comment id={comment_id}")
        return True
    else:
        logger.warning(f"Failed to delete comment id={comment_id}: {r.status_code} {r.text}")
        return False


def _create_or_update_file_comment(owner: str, repo: str, pr_number: str, file_path: str, body: str):
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


# ---------- PR-files fetching ----------
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


# ---------- Building aggregated content ----------
def build_file_section(summary: Dict, pr_number: str) -> str:
    fp = html.escape(summary["file_path"])
    status = summary["status"]
    title = f"**{fp}** — {status}"
    parts = [f"### {title}\n"]
    preview = summary.get("preview") or "(no preview available)"
    parts.append(f"**Preview:**\n\n{preview}\n\n")

    if status in ("added", "removed"):
        content = summary.get("content") or ""
        if content and len(content) > UPLOAD_TO_GIST_THRESHOLD_CHARS:
            gist_url = create_gist({f"{summary['file_path']}.xml": content}, description=f"{summary['file_path']} content for PR {pr_number}")
            if gist_url:
                parts.append(f"Full content is large — view it on a gist: {gist_url}\n\n")
            else:
                lines = content.splitlines()
                chunk = lines[:MAX_LINES_PER_SECTION]
                parts.append("```xml\n" + "\n".join(chunk) + "\n```\n\n")
        else:
            lines = (summary.get("content") or "").splitlines()
            if not lines:
                parts.append("_(No content to show)_\n\n")
            else:
                total = (len(lines) - 1) // MAX_LINES_PER_SECTION + 1
                for i in range(0, len(lines), MAX_LINES_PER_SECTION):
                    chunk = lines[i: i + MAX_LINES_PER_SECTION]
                    part = i // MAX_LINES_PER_SECTION + 1
                    details = (
                        f"<details>\n<summary>Part {part}/{total} — click to expand</summary>\n\n"
                        f"```xml\n" + "\n".join(chunk) + "\n```\n\n</details>\n"
                    )
                    parts.append(details)
    elif status == "modified":
        diff_lines = summary.get("diff_lines") or []
        if not diff_lines:
            parts.append("✅ No meaningful changes detected.\n\n")
        else:
            joined = "\n".join(diff_lines)
            if len(joined) > UPLOAD_TO_GIST_THRESHOLD_CHARS:
                gist_url = create_gist({f"{summary['file_path']}.diff": joined}, description=f"Diff for {summary['file_path']} (PR {pr_number})")
                if gist_url:
                    parts.append(f"Diff is large — view full diff in a gist: {gist_url}\n\n")
                else:
                    parts.append("Diff is large; showing first part below.\n\n")
            total = (len(diff_lines) - 1) // MAX_LINES_PER_SECTION + 1
            for i in range(0, len(diff_lines), MAX_LINES_PER_SECTION):
                chunk = diff_lines[i: i + MAX_LINES_PER_SECTION]
                part = i // MAX_LINES_PER_SECTION + 1
                details = (
                    f"<details>\n<summary>Diff Part {part}/{total} — click to expand</summary>\n\n"
                    f"```diff\n" + "\n".join(chunk) + "\n```\n\n</details>\n"
                )
                parts.append(details)
    else:
        parts.append("_(Unknown status)_\n\n")

    parts.append("\n---\n")
    return "\n".join(parts)


def pack_sections_into_comments(header_tag: str, intro: str, file_sections: List[str], max_chars: int) -> List[str]:
    comments = []
    current = header_tag + "\n\n" + intro + "\n"
    for section in file_sections:
        if len(current) + len(section) > max_chars:
            if current.strip() == (header_tag + "\n\n" + intro).strip() and len(section) > max_chars:
                truncated = section[:max_chars - len(current) - 200]
                current += truncated + "\n\n" + "(truncated — full content in gist)\n"
                comments.append(current)
                current = header_tag + "\n\n" + intro + "\n"
            else:
                comments.append(current)
                current = header_tag + "\n\n" + intro + "\n" + section
        else:
            current += section
    if current.strip():
        comments.append(current)
    if comments:
        comments[-1] += f"\n\n*Tip:* Search for the tag `{header_tag}` to find these comments."
    return comments


# ---------- Fallback helper ----------
def _fallback_post_per_file_and_summary(owner: str, repo: str, pr_number: str, pr_tag: str, file_summaries: List[Dict]):
    """
    Robust fallback: post per-file comments (small snippets) and a tiny PR summary.
    Used when aggregated posting or gist creation fails.
    """
    logger.info("Running fallback: posting per-file comments and short PR summary")
    for s in file_summaries:
        fb_parts = [f"#tableau-file {s['file_path']}\n\n",
                    f"**File:** `{html.escape(s['file_path'])}`\n\n",
                    f"**Status:** {s['status']}\n\n",
                    f"**Preview:**\n\n{s.get('preview') or '(no preview)'}\n\n"]
        inline_text = ""
        if s.get("status") in ("added", "removed"):
            c = s.get("content") or ""
            inline_text = "\n".join(c.splitlines()[:MAX_LINES_PER_SECTION])
        else:
            dl = s.get("diff_lines") or []
            inline_text = "\n".join(dl[:MAX_LINES_PER_SECTION])
        if inline_text:
            fence = "diff" if s.get("status") == "modified" else "xml"
            fb_parts.append(f"```{fence}\n" + inline_text + "\n```\n")
        body_file = "\n".join(fb_parts)
        try:
            _create_or_update_file_comment(owner, repo, pr_number, s['file_path'], body_file)
        except Exception:
            logger.exception(f"Failed fallback posting per-file comment for {s['file_path']}")

    summary_lines = []
    for s in file_summaries:
        first_preview = (s.get('preview') or '').splitlines()[0] if s.get('preview') else '(no preview)'
        summary_lines.append(f"- `{s['file_path']}`: {s['status']} — {first_preview[:120]}")
    tiny = f"{pr_tag}\n\nFull diffs were too large to post inline and gist creation failed or is not permitted.\n\nSummary:\n\n" + "\n".join(summary_lines)
    try:
        _create_pr_comment(owner, repo, pr_number, tiny)
    except Exception:
        logger.exception("Failed to post tiny PR summary comment in fallback")


# ---------- Main orchestration ----------
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

        # Build header and intro
        header_tag = f"{SEARCHABLE_PR_TAG} {pr_number}"
        intro = (
            f"Automated Tableau diff summary for PR **{pr_number}**.\n\n"
            "This comment is managed by the bot and will be replaced on subsequent runs.\n\n"
        )

        # Build per-file sections (strings)
        file_sections = []
        for s in file_summaries:
            section = build_file_section(s, pr_number)
            if len(section) > MAX_COMMENT_CHARS:
                logger.warning(f"Section for {s['file_path']} is larger than MAX_COMMENT_CHARS — creating gist and linking.")
                if s.get("status") in ("added", "removed"):
                    content = s.get("content") or ""
                    gist_url = create_gist({f"{s['file_path']}.xml": content}, description=f"{s['file_path']} content for PR {pr_number}")
                else:
                    diff_lines = s.get("diff_lines") or []
                    gist_url = create_gist({f"{s['file_path']}.diff": "\n".join(diff_lines)}, description=f"Diff for {s['file_path']} (PR {pr_number})")
                if gist_url:
                    small = f"### **{html.escape(s['file_path'])}** — {s['status']}\n\nFull content/diff is large — view: {gist_url}\n\n---\n"
                    file_sections.append(small)
                    continue
                else:
                    section = section[: MAX_COMMENT_CHARS - 200] + "\n\n(truncated — failed to create gist)\n\n---\n"
            file_sections.append(section)

        # Pack sections into multiple comment bodies
        comment_bodies = pack_sections_into_comments(header_tag, intro, file_sections, MAX_COMMENT_CHARS)

        # Clean up any previous bot comments for this PR with same tag
        prev_comments = _list_bot_pr_comments(owner, repo, pr_number, header_tag)
        for pc in prev_comments:
            cid = pc.get("id")
            try:
                _delete_comment(owner, repo, cid)
            except Exception:
                logger.warning(f"Failed deleting old comment id={cid}; continuing.")

        # Post each comment part sequentially, with robust fallback on 422
        posted_comment_ids = []
        aborted_with_422 = False
        for idx, body in enumerate(comment_bodies, start=1):
            part_header = f"{header_tag} — Part {idx}/{len(comment_bodies)}"
            body_with_part = body.replace(header_tag, part_header, 1)
            res = _create_pr_comment(owner, repo, pr_number, body_with_part)
            if isinstance(res, dict) and res.get("error") == 422:
                logger.warning("Detected 422 when creating an aggregated comment part; will fallback to per-file comments.")
                aborted_with_422 = True
                break
            elif isinstance(res, dict) and res.get("error"):
                logger.warning(f"Unexpected error creating comment part: {res}")
            else:
                try:
                    posted_comment_ids.append(res.get("id"))
                except Exception:
                    pass
            time.sleep(0.6)

        if aborted_with_422:
            # cleanup any partially posted aggregated comment parts
            prev_coms = _list_bot_pr_comments(owner, repo, pr_number, header_tag)
            for pc in prev_coms:
                try:
                    _delete_comment(owner, repo, pc.get("id"))
                except Exception:
                    logger.warning(f"Could not delete partial comment id={pc.get('id')}")
            # fallback to per-file + tiny summary
            _fallback_post_per_file_and_summary(owner, repo, pr_number, header_tag, file_summaries)
        else:
            logger.info(f"Posted {len(posted_comment_ids)} aggregated comment parts for PR {pr_number}")

        # Optionally create/update per-file comments as well
        if CREATE_PER_FILE_COMMENTS:
            for s in file_summaries:
                fb_parts = [f"#tableau-file {s['file_path']}\n\n", f"**File:** `{html.escape(s['file_path'])}`\n\n", f"**Status:** {s['status']}\n\n", f"**Preview:**\n\n{s.get('preview') or '(no preview)'}\n\n"]
                inline_text = ""
                if s.get("status") in ("added","removed"):
                    c = s.get("content") or ""
                    if len(c) > UPLOAD_TO_GIST_THRESHOLD_CHARS:
                        gist_url = create_gist({f"{s['file_path']}.xml": c}, description=f"{s['file_path']} content (PR {pr_number})")
                        if gist_url:
                            fb_parts.append(f"Full content: {gist_url}\n\n")
                    else:
                        inline_text = "\n".join((c.splitlines()[:MAX_LINES_PER_SECTION]))
                else:
                    dl = s.get("diff_lines") or []
                    joined = "\n".join(dl)
                    if len(joined) > UPLOAD_TO_GIST_THRESHOLD_CHARS:
                        gist_url = create_gist({f"{s['file_path']}.diff": joined}, description=f"Diff for {s['file_path']} (PR {pr_number})")
                        if gist_url:
                            fb_parts.append(f"Full diff: {gist_url}\n\n")
                    else:
                        inline_text = "\n".join(dl[:MAX_LINES_PER_SECTION])
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

    logger.info(f"Starting splitter bot for {owner}/{repo} PR {pr_number}")

    if not all([owner, repo, pr_number, head_branch, base_branch]):
        logger.error("Missing required environment variables: OWNER, REPO, PR_NUMBER, HEAD_BRANCH, BASE_BRANCH")
        return

    process_pull_request(owner, repo, pr_number, base_branch, head_branch)


if __name__ == "__main__":
    main()
