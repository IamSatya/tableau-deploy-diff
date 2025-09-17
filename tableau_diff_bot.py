#!/usr/bin/env python3
"""
Local Git-based Tableau diff bot.

- No GitHub API calls or token required.
- Computes diffs between origin/<BASE_BRANCH> and HEAD in the checked-out workspace.
- Produces safe-split comment bodies (byte-aware, preserves fenced code blocks)
  and writes them to `comment_bodies.json` as a JSON array of strings.
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
from typing import List, Dict
import subprocess
import json
import difflib
import html

# Configuration from env (set by Jenkinsfile)
SEARCHABLE_PR_TAG = os.getenv("SEARCHABLE_PR_TAG", "#tableau-diff-pr")
MAX_LINES_PER_SECTION = int(os.getenv("MAX_LINES_PER_SECTION", "1000"))
SAFE_COMMENT_CHARS = int(os.getenv("SAFE_COMMENT_CHARS", "60000"))
EXTRACTION_DELAY_THRESHOLD_BYTES = int(os.getenv("EXTRACTION_DELAY_THRESHOLD_BYTES", str(8_000_000)))
EXTRACTION_INITIAL_DELAY_SEC = float(os.getenv("EXTRACTION_INITIAL_DELAY_SEC", "2"))
EXTRACTION_MAX_RETRIES = int(os.getenv("EXTRACTION_MAX_RETRIES", "4"))
EXTRACTION_BACKOFF_FACTOR = float(os.getenv("EXTRACTION_BACKOFF_FACTOR", "2"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tableau-diff-local-bot")


def byte_len(s: str) -> int:
    if s is None:
        return 0
    return len(s.encode("utf-8"))


def run_git(cmd: List[str], check=True, capture_output=True, text=False):
    logger.debug("git: %s", " ".join(cmd))
    res = subprocess.run(["git"] + cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0 and check:
        raise RuntimeError(f"git {' '.join(cmd)} failed rc={res.returncode} err={res.stderr.decode('utf-8', errors='replace')}")
    return res


def list_changed_files(base_ref: str) -> List[Dict]:
    """
    Return list of dicts similar to GitHub PR files: {filename, status}
    status one of: added, removed, modified, renamed (we treat renamed as modified)
    """
    # use name-status to get codes: A, D, M, R<score>
    res = run_git(["diff", "--name-status", f"{base_ref}...HEAD"])
    out = res.stdout.decode("utf-8", errors="replace").strip()
    files = []
    if not out:
        return files
    for ln in out.splitlines():
        parts = ln.strip().split("\t")
        code = parts[0]
        if code.startswith("R"):
            # rename: "R100\told\tnew"
            if len(parts) >= 3:
                filename = parts[2]
                status = "modified"
            else:
                continue
        else:
            if len(parts) < 2:
                continue
            filename = parts[1]
            if code == "A":
                status = "added"
            elif code == "D":
                status = "removed"
            elif code == "M":
                status = "modified"
            else:
                status = "modified"
        files.append({"filename": filename, "status": status})
    return files


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


# splitting utilities (same logic as previous safe-splitter)
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
                chunks.append("".join(cur_lines)); cur_lines = []; cur_bytes = 0
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
            cur_lines.append(ln); cur_bytes += ln_bytes
        else:
            if cur_lines:
                chunks.append("".join(cur_lines))
            cur_lines = [ln]; cur_bytes = ln_bytes
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
                chunks.append("".join(cur_lines)); cur_lines = []; cur_bytes = 0
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
            cur_lines.append(ln); cur_bytes += ln_b
        else:
            if cur_lines:
                chunks.append("".join(cur_lines))
            cur_lines = [ln]; cur_bytes = ln_b
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
            final_pieces.extend(split_into_chunks(p, max_bytes))
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


def build_file_section(summary: Dict) -> str:
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


def git_show_to_file(ref: str, path: str, out_path: str) -> bool:
    """
    Write file content at ref:path to out_path. Return True if file written.
    """
    # git show may fail for removed files
    cmd = ["show", f"{ref}:{path}"]
    res = subprocess.run(["git"] + cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        return False
    with open(out_path, "wb") as f:
        f.write(res.stdout)
    return True


def process_pr_local(base_branch: str, head_ref: str, pr_number: str) -> List[str]:
    # Ensure base_ref is in form origin/<base> (we fetched it in Jenkins)
    base_ref = f"origin/{base_branch}" if not base_branch.startswith("origin/") else base_branch
    files = list_changed_files(base_ref)
    logger.info("Detected %d file(s) changed", len(files))

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
                got = git_show_to_file(base_ref, file_path, old_path)
                if got:
                    old_xml = extract_twb_content_with_retries(old_path, file_path)
                else:
                    logger.warning("Old version not found for %s@%s", file_path, base_ref)

            if status != "removed":
                # HEAD may be referred as HEAD or the branch name; using HEAD is safest
                got2 = git_show_to_file("HEAD", file_path, new_path)
                if got2:
                    new_xml = extract_twb_content_with_retries(new_path, file_path)
                else:
                    logger.warning("New version not found for %s@HEAD", file_path)

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

    header_tag = f"{SEARCHABLE_PR_TAG} {pr_number}"
    intro = (
        f"Automated Tableau diff summary for PR **{pr_number}**.\n\n"
        "This comment is managed by the bot and will be replaced on subsequent runs.\n\n"
    )

    file_sections = []
    for s in file_summaries:
        file_sections.append(build_file_section(s))

    # pack into comment bodies
    comment_bodies = pack_sections_to_comment_bodies(header_tag, intro, file_sections, SAFE_COMMENT_CHARS)
    return comment_bodies


def main():
    owner = os.getenv("OWNER")
    repo = os.getenv("REPO")
    pr_number = os.getenv("PR_NUMBER")
    head_branch = os.getenv("HEAD_BRANCH")
    base_branch = os.getenv("BASE_BRANCH")

    logger.info("Starting local diff bot for %s/%s PR %s (head=%s base=%s)", owner, repo, pr_number, head_branch, base_branch)

    missing = [k for k, v in (("OWNER", owner), ("REPO", repo), ("PR_NUMBER", pr_number), ("HEAD_BRANCH", head_branch), ("BASE_BRANCH", base_branch)) if not v]
    if missing:
        logger.error("Missing required env vars: %s", missing)
        return

    # verify git has base ref; list_changed_files will error if not present
    try:
        bodies = process_pr_local(base_branch, head_branch, pr_number)
    except Exception as e:
        logger.exception("git diff / processing failed: %s", e)
        bodies = []

    if not bodies:
        # fallback tiny summary so Jenkins can still post something
        tiny = f"{SEARCHABLE_PR_TAG} {pr_number}\n\nNo tableau diffs generated (no .twb/.twbx changes detected or extraction failed)."
        bodies = [tiny]

    # write comment_bodies.json (array of strings)
    out_file = os.getenv("SAFE_COMMENT_JSON", "comment_bodies.json")
    try:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(bodies, f, ensure_ascii=False, indent=2)
        logger.info("Saved %d comment body parts into %s", len(bodies), out_file)
    except Exception:
        logger.exception("Failed to write %s", out_file)


if __name__ == "__main__":
    main()

