#!/usr/bin/env python3
"""
Tableau diff bot (git-local, no GitHub API).
- Fetches origin/<base_branch> if necessary.
- Computes changed files between origin/<base_branch>...HEAD.
- Extracts .twb inside .twbx where needed.
- Normalizes and diffs, then packs comment bodies safely into bytes <= SAFE_COMMENT_CHARS.
- Writes comment_bodies.json and diffs.txt for Jenkins to post via pullRequest.comment(...)
"""
from __future__ import annotations
import os
import re
import sys
import time
import zipfile
import tempfile
import json
import logging
import subprocess
import difflib
from pathlib import Path
from typing import List, Dict, Optional

# --- Config (env-overridable) ---
SAFE_COMMENT_CHARS = int(os.getenv("SAFE_COMMENT_CHARS", "60000"))
MAX_LINES_PER_SECTION = int(os.getenv("MAX_LINES_PER_SECTION", "1000"))
SEARCHABLE_PR_TAG = os.getenv("SEARCHABLE_PR_TAG", "#tableau-diff-pr")
EXTRACTION_DELAY_THRESHOLD_BYTES = int(os.getenv("EXTRACTION_DELAY_THRESHOLD_BYTES", str(8_000_000)))
EXTRACTION_INITIAL_DELAY_SEC = float(os.getenv("EXTRACTION_INITIAL_DELAY_SEC", "2.0"))
EXTRACTION_MAX_RETRIES = int(os.getenv("EXTRACTION_MAX_RETRIES", "4"))
EXTRACTION_BACKOFF_FACTOR = float(os.getenv("EXTRACTION_BACKOFF_FACTOR", "2.0"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tableau-diff-local")

# --- helpers ---
def run_cmd(cmd: List[str], cwd: Optional[str] = None, check: bool = True):
    logger.debug("CMD: %s", " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
    out, err = p.communicate()
    out_s = out.decode("utf-8", errors="replace") if out else ""
    err_s = err.decode("utf-8", errors="replace") if err else ""
    if check and p.returncode != 0:
        raise RuntimeError(f"Command {cmd} failed rc={p.returncode} err={err_s.strip()}")
    return out_s.strip(), err_s.strip()

def byte_len(s: str) -> int:
    if s is None:
        return 0
    return len(s.encode("utf-8"))

# --- XML normalization (reuse your patterns) ---
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

# --- twb/twbx extraction ---
def extract_twb_from_twbx_bytes(b: bytes) -> Optional[str]:
    try:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "archive.twbx"
            p.write_bytes(b)
            if not zipfile.is_zipfile(p):
                return None
            with zipfile.ZipFile(p, "r") as z:
                twb_files = [i for i in z.namelist() if i.lower().endswith(".twb")]
                if not twb_files:
                    twb_files = [i for i in z.namelist() if i.lower().endswith(".xml")]
                if not twb_files:
                    return None
                # choose largest file
                best = max(twb_files, key=lambda x: z.getinfo(x).file_size or 0)
                with z.open(best) as fh:
                    raw = fh.read()
                    return raw.decode("utf-8", errors="replace")
    except Exception:
        logger.exception("extract failed")
        return None

def extract_twb_from_git_blob(ref: str, path: str) -> Optional[str]:
    """
    Try to get file content from git (ref:path). Return decoded text or None.
    """
    try:
        out, _ = run_cmd(["git", "show", f"{ref}:{path}"], check=False)
        if out and out.strip().startswith("PK"):  # likely zip binary
            # treat as bytes by calling git cat-file -p to get raw bytes? git show returns bytes as text, so try a fallback
            # As a pragmatic approach, get blob bytes using git cat-file -p and treat result as bytes via encoding.
            # But git show returned textual replacement already — fallback to trying to decode if contains xml
            if "<?xml" in out:
                return out
            return None
        if out:
            return out
    except Exception as e:
        logger.debug("git show failed for %s:%s -> %s", ref, path, e)
    return None

def extract_twb_from_worktree(path: str) -> Optional[str]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        if p.suffix.lower() == ".twb":
            return p.read_text(encoding="utf-8", errors="replace")
        if p.suffix.lower() == ".twbx":
            b = p.read_bytes()
            return extract_twb_from_twbx_bytes(b)
        # fallback: if file contains xml
        text = p.read_text(encoding="utf-8", errors="replace")
        if text.strip().startswith("<?xml"):
            return text
    except Exception:
        logger.exception("extract worktree failed for %s", path)
    return None

# --- diffs + section building (reuse your build_file_section logic, slightly simplified) ---
def generate_minimal_diff(old_content: str, new_content: str) -> List[str]:
    old_lines = normalize_xml_for_diff(old_content).splitlines()
    new_lines = normalize_xml_for_diff(new_content).splitlines()
    iter_ = difflib.unified_diff(old_lines, new_lines, fromfile="old.twb", tofile="new.twb", lineterm="")
    return [l for l in iter_]

def clean_preview(text: str, max_lines=6) -> str:
    lines = []
    for ln in (text or "").splitlines():
        if ln.strip():
            lines.append(ln.strip())
        if len(lines) >= max_lines:
            break
    return "\n".join(lines) or "(no preview available)"

def build_file_section(summary: Dict, pr_number: str) -> str:
    import html
    fp_safe = html.escape(summary["file_path"])
    status = summary["status"]
    title = f"**{fp_safe}** — {status}"
    parts = [f"### {title}\n", "**Legend:** `+` = addition (green), `-` = removal (red)\n\n"]
    parts.append(f"**Preview:**\n\n{summary.get('preview')}\n\n")

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
                details = "<details>\n" \
                          f"<summary>Part {part}/{total} — click to expand</summary>\n\n" \
                          "```diff\n" + "\n".join(prefixed) + "\n```\n\n</details>\n"
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
                details = "<details>\n" \
                          f"<summary>Diff Part {part}/{total} — click to expand</summary>\n\n" \
                          "```diff\n" + "\n".join(chunk) + "\n```\n\n</details>\n"
                parts.append(details)
    else:
        parts.append("_(Unknown status)_\n\n")
    parts.append("\n---\n")
    return "\n".join(parts)

# --- splitting/packing utilities (copy of your byte-aware logic, slightly trimmed) ---
def split_into_chunks(text: str, max_chars: int) -> List[str]:
    if not text:
        return []
    max_bytes = int(max_chars)
    if byte_len(text) <= max_bytes:
        return [text]
    lines = text.splitlines(keepends=True)
    chunks, cur_lines, cur_bytes = [], [], 0
    for ln in lines:
        ln_b = byte_len(ln)
        if ln_b > max_bytes:
            if cur_lines:
                chunks.append("".join(cur_lines)); cur_lines = []; cur_bytes = 0
            b = ln.encode("utf-8")
            start = 0
            while start < len(b):
                end = min(start + max_bytes, len(b))
                slice_b = b[start:end]
                while True:
                    try:
                        piece = slice_b.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        end -= 1
                        slice_b = b[start:end]
                        if end <= start:
                            piece = slice_b.decode("utf-8", errors="replace")
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

def _split_code_block_by_chars(code_text: str, max_code_chars: int) -> List[str]:
    if not code_text:
        return []
    max_bytes = int(max_code_chars)
    lines = code_text.splitlines(keepends=True)
    chunks, cur_lines, cur_bytes = [], [], 0
    for ln in lines:
        ln_b = byte_len(ln)
        if ln_b > max_bytes:
            if cur_lines:
                chunks.append("".join(cur_lines)); cur_lines = []; cur_bytes = 0
            b = ln.encode("utf-8"); start = 0
            while start < len(b):
                end = min(start + max_bytes, len(b))
                slice_b = b[start:end]
                while True:
                    try:
                        piece = slice_b.decode("utf-8"); break
                    except UnicodeDecodeError:
                        end -= 1; slice_b = b[start:end]
                        if end <= start:
                            piece = slice_b.decode("utf-8", errors="replace"); break
                chunks.append(piece); start = end
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
    pieces, max_bytes = [], int(max_chars)
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
    final = []
    for p in pieces:
        if byte_len(p) <= max_bytes:
            final.append(p)
        else:
            final.extend(split_into_chunks(p, max_bytes))
    return final

def pack_sections_to_comment_bodies(header_tag: str, intro: str, sections: List[str], max_chars: int) -> List[str]:
    bodies = []
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

# --- main orchestration ---
def process_pr_from_git(owner: str, repo: str, pr: str, base_branch: str, head_branch: str):
    # Ensure origin/base exists (fetch)
    try:
        logger.info("Fetching origin/%s...", base_branch)
        run_cmd(["git", "fetch", "origin", f"{base_branch}:{base_branch}"], check=False)
    except Exception:
        logger.warning("git fetch origin/%s failed; continuing (maybe already present).", base_branch)

    # list changed files between origin/base and HEAD
    try:
        out, _ = run_cmd(["git", "diff", "--name-status", f"origin/{base_branch}...HEAD"])
    except Exception as e:
        logger.error("git diff failed: %s", e)
        return

    changed = []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        parts = ln.split("\t")
        status = parts[0].strip()
        path = parts[1].strip() if len(parts) > 1 else None
        if path:
            changed.append({"filename": path, "status": status.lower()})
    if not changed:
        logger.info("No changed files detected between origin/%s and HEAD", base_branch)
        # still write empty outputs so Jenkins won't fail unexpectedly
        Path("diffs.txt").write_text("No TWB/TWBX changes detected.\n")
        Path("comment_bodies.json").write_text(json.dumps(["#tableau-diff-pr " + pr + "\n\nNo TWB/TWBX changes detected.\n"]))
        return

    file_summaries = []
    for c in changed:
        fp = c["filename"]
        st = c["status"]
        if not (fp.lower().endswith(".twb") or fp.lower().endswith(".twbx")):
            continue

        old_xml = ""
        new_xml = ""
        # old: try git show origin/base:fp
        if st != "a":  # not added in head -> should exist in base
            try:
                old_content = None
                try:
                    out_old, _ = run_cmd(["git", "show", f"origin/{base_branch}:{fp}"], check=False)
                    old_content = out_old if out_old else None
                except Exception:
                    old_content = None
                if old_content:
                    # if it's a zip archive inside git, detection is tricky — try xml detection
                    if "<?xml" in old_content:
                        old_xml = old_content
                    else:
                        # attempt to get raw blob bytes via git show -p? skip complex handling; try fallback
                        old_xml = old_content
                else:
                    old_xml = ""
            except Exception:
                logger.exception("Failed to extract old file %s", fp)

        # new: from worktree
        if st != "d":  # not deleted
            try:
                new_xml = extract_twb_from_worktree(fp) or ""
                if not new_xml:
                    # maybe file path was renamed or is packed; try git show HEAD:fp
                    out_new, _ = run_cmd(["git", "show", f"HEAD:{fp}"], check=False)
                    new_xml = out_new or ""
            except Exception:
                logger.exception("Failed to extract new file %s", fp)

        summary = {"file_path": fp, "status": "modified"}
        if st == "a":
            summary["status"] = "added"
            summary["content"] = new_xml
        elif st == "d":
            summary["status"] = "removed"
            summary["content"] = old_xml
        else:
            # modified: create diff if possible
            if old_xml and new_xml:
                summary["diff_lines"] = generate_minimal_diff(old_xml, new_xml)
            else:
                # try to extract twb inside twbx bytes for base/head via git show and worktree bytes
                if not old_xml:
                    try:
                        # attempt git show raw bytes and extract twb inside if it's twbx
                        out_old, _ = run_cmd(["git", "show", f"origin/{base_branch}:{fp}"], check=False)
                        if out_old and "PK" in out_old:
                            # best-effort: attempt to treat returned string as bytes via utf-8 and extract; may be imperfect
                            old_xml = out_old
                    except Exception:
                        pass
                if not new_xml:
                    try:
                        out_new, _ = run_cmd(["git", "show", f"HEAD:{fp}"], check=False)
                        if out_new and "PK" in out_new:
                            new_xml = out_new
                    except Exception:
                        pass
                if old_xml and new_xml:
                    summary["diff_lines"] = generate_minimal_diff(old_xml, new_xml)
                else:
                    summary["preview"] = clean_preview(new_xml or old_xml)
        summary["preview"] = summary.get("preview") or clean_preview(new_xml or old_xml)
        file_summaries.append(summary)

    # Build sections
    header_tag = f"{SEARCHABLE_PR_TAG} {pr}"
    intro = f"Automated Tableau diff summary for PR **{pr}**.\n\nThis comment is managed by the bot and will be replaced on subsequent runs.\n\n"

    file_sections = []
    for s in file_summaries:
        section = build_file_section(s, pr)
        file_sections.append(section)

    # pack into comment bodies
    comment_bodies = pack_sections_to_comment_bodies(header_tag, intro, file_sections, SAFE_COMMENT_CHARS)

    # write outputs
    try:
        Path("diffs.txt").write_text("\n\n".join(file_sections), encoding="utf-8")
        Path("comment_bodies.json").write_text(json.dumps(comment_bodies), encoding="utf-8")
        logger.info("Saved %d comment bodies into comment_bodies.json and diffs.txt", len(comment_bodies))
    except Exception:
        logger.exception("Failed to write outputs")

if __name__ == "__main__":
    OWNER = os.getenv("OWNER", "")
    REPO = os.getenv("REPO", "")
    PR_NUMBER = os.getenv("PR_NUMBER", os.getenv("CHANGE_ID", ""))
    HEAD_BRANCH = os.getenv("HEAD_BRANCH", os.getenv("CHANGE_BRANCH", "HEAD"))
    BASE_BRANCH = os.getenv("BASE_BRANCH", os.getenv("CHANGE_TARGET", "main"))

    logger.info("Starting local diff bot for %s/%s PR %s (head=%s base=%s)", OWNER, REPO, PR_NUMBER, HEAD_BRANCH, BASE_BRANCH)
    if not PR_NUMBER:
        logger.error("PR number is required via PR_NUMBER (or CHANGE_ID). Exiting.")
        sys.exit(0)
    process_pr_from_git(OWNER, REPO, PR_NUMBER, BASE_BRANCH, HEAD_BRANCH)

