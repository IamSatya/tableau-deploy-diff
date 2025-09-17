#!/usr/bin/env python3
"""
Tableau diff bot — local-git version.

This script computes diffs between HEAD and origin/<base> using git commands locally,
extracts .twb/.twbx contents, generates colorized diff sections, packs into safely
split comment bodies (byte-aware), and writes comment_bodies.json (array of strings).

It intentionally does NOT call the GitHub API. Comment posting is expected to be
performed by the Jenkins pipeline via pullRequest.comment(String).
"""
import os
import re
import time
import zipfile
import tempfile
import base64
import traceback
import logging
import subprocess
from pathlib import Path
from typing import Optional, List, Dict

import difflib
from dotenv import load_dotenv
import html
import json

load_dotenv()

# Behavior config
MAX_LINES_PER_SECTION = int(os.getenv("MAX_LINES_PER_SECTION", "1000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SEARCHABLE_PR_TAG = os.getenv("SEARCHABLE_PR_TAG", "#tableau-diff-pr")

# Extraction delay/retry config
EXTRACTION_DELAY_THRESHOLD_BYTES = int(os.getenv("EXTRACTION_DELAY_THRESHOLD_BYTES", str(8_000_000)))  # 8MB
EXTRACTION_INITIAL_DELAY_SEC = float(os.getenv("EXTRACTION_INITIAL_DELAY_SEC", "2"))
EXTRACTION_MAX_RETRIES = int(os.getenv("EXTRACTION_MAX_RETRIES", "4"))
EXTRACTION_BACKOFF_FACTOR = float(os.getenv("EXTRACTION_BACKOFF_FACTOR", "2"))

# Comment splitting config (bytes)
SAFE_COMMENT_CHARS = int(os.getenv("SAFE_COMMENT_CHARS", "60000"))

# logging
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tableau-diff-local-git")

# ---------- Helper: byte-length aware ----------
def byte_len(s: str) -> int:
    """Return length in bytes for UTF-8 encoding."""
    if s is None:
        return 0
    return len(s.encode("utf-8"))


# ---------- Extraction and normalization (unchanged) ----------
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
        (re.compile(r'(<workbook.*?project-luid=")[^"]+(")'), r"\1<redacted>\2"),
        (re.compile(r'(<datasource.*?luid=")[^"]+(")'), r"\1<redacted>\2"),
        (re.compile(r'(<connection.*?id=")[^"]+(")'), r"\1<redacted>\2"),
        (re.compile(r'(<uid>)[^<]+(</uid>)'), r"\1<redacted>\2"),
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


# ---------- Diff generation (full unified diff) ----------
def generate_minimal_diff(old_content: str, new_content: str) -> List[str]:
    old_lines = normalize_xml_for_diff(old_content).splitlines()
    new_lines = normalize_xml_for_diff(new_content).splitlines()
    diff_iter = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="old.twb",
        tofile="new.twb",
        lineterm=""
    )
    return [line for line in diff_iter]


# ---------- Git helpers (local) ----------
def run_git(cmd: List[str], check: bool = True) -> str:
    """Run git command and return stdout text (utf-8)."""
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        return out.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        logger.debug(f"git command failed: {' '.join(cmd)} -> rc={e.returncode}")
        logger.debug(e.output.decode("utf-8", errors="replace"))
        if check:
            raise
        return ""


def get_changed_files_from_git(base_branch: str) -> List[Dict]:
    """
    Returns list of dicts like {"filename": path, "status": "added|modified|removed"}
    Uses origin/<base_branch>...HEAD (three-dot) range to find changes introduced by the head.
    """
    if base_branch:
        base_ref = f"origin/{base_branch}"
    else:
        base_ref = "origin/main"
    # Use --name-status for simple status and path
    cmd = ["git", "diff", "--name-status", f"{base_ref}...HEAD"]
    out = run_git(cmd, check=False)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    results = []
    for ln in lines:
        # status can be "A", "M", "D", "R100" etc. We care about A/M/D
        parts = ln.split("\t")
        if not parts:
            continue
        status = parts[0]
        # for renames the filename may be two fields; we take the new path
        if status.startswith("R") and len(parts) >= 3:
            path = parts[2]
            st = "modified"
        else:
            path = parts[1] if len(parts) >= 2 else None
            st = {"A": "added", "M": "modified", "D": "removed"}.get(status, "modified")
        if path:
            results.append({"filename": path, "status": st})
    return results


def git_show_blob(ref: str, path: str) -> Optional[bytes]:
    """Return raw bytes of file at ref:path (or None if not found)."""
    try:
        out = subprocess.check_output(["git", "show", f"{ref}:{path}"], stderr=subprocess.STDOUT)
        return out
    except subprocess.CalledProcessError:
        return None


# ---------- Build file section (colorized add/remove) ----------
def build_file_section(summary: Dict, pr_number: str) -> str:
    fp_safe = html.escape(summary["file_path"])
    status = summary["status"]
    title = f"**{fp_safe}** — {status}"
    parts = [f"### {title}\n"]

    parts.append("**Legend:** `+` = addition (green), `-` = removal (red)\n\n")

    preview = summary.get("preview") or "(no preview available)"
    parts.append(f"**Preview:**\n\n{preview}\n\n")

    def clean_line(ln: str) -> str:
        if ln and ln[0] == "\ufeff":
            ln = ln[1:]
        return ln.rstrip("\r")

    if status in ("added", "removed"):
        content = summary.get("content") or ""
        lines = content.splitlines()
        if not lines:
            parts.append("_(No content to show)_\n\n")
        else:
            total = (len(lines) - 1) // MAX_LINES_PER_SECTION + 1
            for i in range(0, len(lines), MAX_LINES_PER_SECTION):
                chunk = lines[i: i + MAX_LINES_PER_SECTION]
                part = i // MAX_LINES_PER_SECTION + 1
                if status == "added":
                    prefixed = ["+" + clean_line(ln) for ln in chunk]
                else:
                    prefixed = ["-" + clean_line(ln) for ln in chunk]
                details = (
                    "<details>\n"
                    f"<summary>Part {part}/{total} — click to expand</summary>\n\n"
                    "```diff\n"
                    + "\n".join(prefixed) +
                    "\n```\n\n"
                    "</details>\n"
                )
                parts.append(details)
    elif status == "modified":
        diff_lines = summary.get("diff_lines") or []
        if not diff_lines:
            parts.append("✅ No meaningful changes detected.\n\n")
        else:
            cleaned = []
            for l in diff_lines:
                if l and l[0] == "\ufeff":
                    l = l[1:]
                cleaned.append(l.rstrip("\r"))
            total = (len(cleaned) - 1) // MAX_LINES_PER_SECTION + 1
            for i in range(0, len(cleaned), MAX_LINES_PER_SECTION):
                chunk = cleaned[i: i + MAX_LINES_PER_SECTION]
                part = i // MAX_LINES_PER_SECTION + 1
                details = (
                    "<details>\n"
                    f"<summary>Diff Part {part}/{total} — click to expand</summary>\n\n"
                    "```diff\n"
                    + "\n".join(chunk) +
                    "\n```\n\n"
                    "</details>\n"
                )
                parts.append(details)
    else:
        parts.append("_(Unknown status)_\n\n")

    parts.append("\n---\n")
    return "\n".join(parts)


# ---------- Splitting utilities that preserve fences (BYTE-BASED) ----------
def split_into_chunks(text: str, max_chars: int) -> List[str]:
    if not text:
        return []
    max_bytes = int(max_chars)
    if byte_len(text) <= max_bytes:
        return [text]

    lines = text.splitlines(keepends=True)
    chunks: List[str] = []
    cur_lines: List[str] = []
    cur_bytes = 0
    for ln in lines:
        ln_bytes = byte_len(ln)
        if ln_bytes > max_bytes:
            if cur_lines:
                chunks.append("".join(cur_lines))
                cur_lines = []
                cur_bytes = 0
            b = ln.encode("utf-8")
            start = 0
            while start < len(b):
                end = min(start + max_bytes, len(b))
                slice_bytes = b[start:end]
                while True:
                    try:
                        piece = slice_bytes.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        end -= 1
                        slice_bytes = b[start:end]
                        if end <= start:
                            piece = slice_bytes.decode("utf-8", errors="replace")
                            break
                chunks.append(piece)
                start = end
            continue

        if cur_bytes + ln_bytes <= max_bytes:
            cur_lines.append(ln)
            cur_bytes += ln_bytes
        else:
            if cur_lines:
                chunks.append("".join(cur_lines))
            cur_lines = [ln]
            cur_bytes = ln_bytes
    if cur_lines:
        chunks.append("".join(cur_lines))
    return chunks


def _split_code_block_by_chars(code_text: str, max_code_chars: int) -> List[str]:
    if not code_text:
        return []
    max_bytes = int(max_code_chars)
    lines = code_text.splitlines(keepends=True)
    chunks: List[str] = []
    cur_lines: List[str] = []
    cur_bytes = 0
    for ln in lines:
        ln_b = byte_len(ln)
        if ln_b > max_bytes:
            if cur_lines:
                chunks.append("".join(cur_lines))
                cur_lines = []
                cur_bytes = 0
            b = ln.encode("utf-8")
            start = 0
            while start < len(b):
                end = min(start + max_bytes, len(b))
                slice_bytes = b[start:end]
                while True:
                    try:
                        piece = slice_bytes.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        end -= 1
                        slice_bytes = b[start:end]
                        if end <= start:
                            piece = slice_bytes.decode("utf-8", errors="replace")
                            break
                chunks.append(piece)
                start = end
            continue
        if cur_bytes + ln_b <= max_bytes:
            cur_lines.append(ln)
            cur_bytes += ln_b
        else:
            if cur_lines:
                chunks.append("".join(cur_lines))
            cur_lines = [ln]
            cur_bytes = ln_b
    if cur_lines:
        chunks.append("".join(cur_lines))
    return chunks


def split_section_preserve_fences(section: str, max_chars: int) -> List[str]:
    if not section:
        return []
    pieces: List[str] = []
    max_bytes = int(max_chars)

    fence_pat = re.compile(r'```diff\n(.*?)\n```', re.DOTALL)
    last = 0
    for m in fence_pat.finditer(section):
        pre = section[last:m.start()]
        if pre:
            pieces.extend(split_into_chunks(pre, max_bytes))
        code_inner = m.group(1)
        overhead = byte_len("```diff\n") + byte_len("\n```")
        safe_code_bytes = max(200, max_bytes - overhead - 200)
        code_chunks = _split_code_block_by_chars(code_inner, safe_code_bytes)
        for cc in code_chunks:
            fenced = "```diff\n" + cc + "\n```"
            if byte_len(fenced) <= max_bytes:
                pieces.append(fenced)
            else:
                inner_chunks = _split_code_block_by_chars(cc, max_bytes - overhead)
                for ic in inner_chunks:
                    pieces.append("```diff\n" + ic + "\n```")
        last = m.end()
    tail = section[last:]
    if tail:
        pieces.extend(split_into_chunks(tail, max_bytes))

    final_pieces: List[str] = []
    for p in pieces:
        if byte_len(p) <= max_bytes:
            final_pieces.append(p)
        else:
            chunks = split_into_chunks(p, max_bytes)
            final_pieces.extend(chunks)
    return final_pieces


def pack_sections_to_comment_bodies(header_tag: str, intro: str, sections: List[str], max_chars: int) -> List[str]:
    bodies: List[str] = []
    max_bytes = int(max_chars)
    header_and_intro = header_tag + "\n\n" + intro + "\n"
    current = header_and_intro
    for section in sections:
        safe_parts = split_section_preserve_fences(section, max_bytes)
        for part in safe_parts:
            part = re.sub(r'^\s+```', '```', part, flags=re.MULTILINE)
            if byte_len(current) + byte_len(part) <= max_bytes:
                current += part
            else:
                bodies.append(current)
                current = header_and_intro + part
                if byte_len(current) > max_bytes:
                    chunks = split_into_chunks(current, max_bytes)
                    bodies.extend(chunks[:-1])
                    current = chunks[-1]
    if current.strip():
        bodies.append(current)
    if bodies:
        bodies[-1] += f"\n\n*Tip:* Search for the tag `{header_tag}` to find these comments."
    return bodies


# ---------- Fallback per-file is omitted (Jenkins will post aggregated parts) ----------
def _fallback_post_per_file_and_summary(*args, **kwargs):
    # kept for parity but Jenkins will perform posting; the Python bot only creates bodies.
    logger.info("Fallback would have posted per-file; not performed here.")


# ---------- Main orchestration (git-based) ----------
def process_pull_request(owner: str, repo: str, pr_number: str, base_branch: str, head_branch: str):
    try:
        # Discover changed files via git
        changes = get_changed_files_from_git(base_branch)
        if not changes:
            logger.info("No changed files detected by git diff")
            # still create an empty JSON array to signal no posts
            with open("comment_bodies.json", "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
            return

        logger.info(f"Detected {len(changes)} file(s) changed")

        file_summaries = []
        for change in changes:
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

                # base (origin/<base>)
                if status != "added":
                    try:
                        blob = git_show_blob(f"origin/{base_branch}", file_path)
                        if blob is not None:
                            with open(old_path, "wb") as f:
                                f.write(blob)
                            old_xml = extract_twb_content_with_retries(old_path, file_path)
                        else:
                            logger.warning(f"Old blob not found in origin/{base_branch}: {file_path}")
                    except Exception:
                        logger.exception("Failed to obtain old file from git")

                # head (HEAD)
                if status != "removed":
                    try:
                        blob = git_show_blob("HEAD", file_path)
                        if blob is not None:
                            with open(new_path, "wb") as f:
                                f.write(blob)
                            new_xml = extract_twb_content_with_retries(new_path, file_path)
                        else:
                            # file might be present on disk (if workspace checked out)
                            if os.path.exists(file_path):
                                with open(file_path, "rb") as f:
                                    b = f.read()
                                with open(new_path, "wb") as f:
                                    f.write(b)
                                new_xml = extract_twb_content_with_retries(new_path, file_path)
                            else:
                                logger.warning(f"New blob not found in HEAD: {file_path}")
                    except Exception:
                        logger.exception("Failed to obtain new file from git")

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
            file_sections.append(section)

        # Pack sections into multiple comment bodies (each <= SAFE_COMMENT_CHARS bytes)
        comment_bodies = pack_sections_to_comment_bodies(header_tag, intro, file_sections, SAFE_COMMENT_CHARS)

        # Save comment bodies to JSON for Jenkins to read & post
        with open("comment_bodies.json", "w", encoding="utf-8") as f:
            json.dump(comment_bodies, f, ensure_ascii=False, indent=2)

        # Also save diffs.txt for debugging
        with open("diffs.txt", "w", encoding="utf-8") as f:
            f.write("\n\n---\n\n".join(file_sections))

        logger.info(f"Saved {len(comment_bodies)} comment body parts into comment_bodies.json")

    except Exception:
        logger.exception("Error in process_pull_request")


def main():
    owner = os.getenv("OWNER")
    repo = os.getenv("REPO")
    pr_number = os.getenv("PR_NUMBER")
    head_branch = os.getenv("HEAD_BRANCH") or os.getenv("CHANGE_BRANCH") or os.getenv("BRANCH_NAME")
    base_branch = os.getenv("BASE_BRANCH") or os.getenv("CHANGE_TARGET") or "main"

    logger.info(f"Starting local diff bot for {owner}/{repo} PR {pr_number} (head={head_branch} base={base_branch})")

    if not all([owner, repo, pr_number, head_branch, base_branch]):
        logger.error("Missing required environment variables: OWNER, REPO, PR_NUMBER, HEAD_BRANCH, BASE_BRANCH")
        # still write empty array so Jenkins can detect failure gracefully
        with open("comment_bodies.json", "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False)
        return

    process_pull_request(owner, repo, pr_number, base_branch, head_branch)


if __name__ == "__main__":
    main()

