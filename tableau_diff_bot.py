#!/usr/bin/env python3
import os
import sys
import json
import difflib
import zipfile
import tempfile
import logging
import html
from pathlib import Path
import subprocess
from typing import List

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

COMMENT_FILE = os.getenv("SAFE_COMMENT_JSON", "comment_bodies.json")
SAFE_COMMENT_SIZE = int(os.getenv("SAFE_COMMENT_SIZE", "60000"))  # bytes
MAX_LINES_PER_PART = int(os.getenv("MAX_LINES_PER_PART", "2000"))  # fallback
# minimal internal allowance for fenced wrapper bytes
FENCED_OVERHEAD_BYTES = len("```diff\n") + len("\n```")

def byte_len(s: str) -> int:
    if s is None:
        return 0
    return len(s.encode("utf-8"))

def extract_file(path, workdir):
    """
    If .twbx, extract the embedded .twb file to a temp dir.
    Otherwise, return the file path unchanged.
    """
    if not path:
        return None
    if path.lower().endswith(".twbx"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".twb"):
                        extracted = Path(workdir) / Path(name).name
                        with zf.open(name) as src, open(extracted, "wb") as dst:
                            dst.write(src.read())
                        logging.info("[extract] Extracted %s -> %s", name, extracted)
                        return str(extracted)
        except Exception as e:
            logging.warning("Failed extracting twbx %s: %s", path, e)
            return None
        return None
    return path

def run_diff(file_a, file_b):
    """
    Generate a unified diff between two files.
    Returns diff lines as a list of strings (no trailing newlines).
    """
    try:
        with open(file_a, encoding="utf-8", errors="ignore") as f1, \
             open(file_b, encoding="utf-8", errors="ignore") as f2:
            a_lines = [l.rstrip("\n") for l in f1.readlines()]
            b_lines = [l.rstrip("\n") for l in f2.readlines()]
    except Exception as e:
        logging.error("Failed to read files %s vs %s: %s", file_a, file_b, e)
        return []

    diff_iter = difflib.unified_diff(
        a_lines, b_lines,
        fromfile=Path(file_a).name, tofile=Path(file_b).name,
        lineterm=""
    )
    return list(diff_iter)

def chunk_lines_by_bytes(lines: List[str], max_bytes: int) -> List[List[str]]:
    """
    Byte-aware chunking of list-of-lines into groups such that the
    wrapped fenced block (```diff\n + chunk + \n```) will be <= max_bytes.
    """
    groups = []
    cur = []
    cur_bytes = 0
    safe_max = max(max_bytes - FENCED_OVERHEAD_BYTES - 200, 200)  # leave wiggle room

    for ln in lines:
        ln_with_n = ln + "\n"
        ln_b = byte_len(ln_with_n)
        # If a single line is larger than safe_max, break it into smaller byte-safe pieces.
        if ln_b > safe_max:
            # flush current
            if cur:
                groups.append(cur)
                cur = []
                cur_bytes = 0
            # force-slice the long line at byte boundaries
            b = ln_with_n.encode("utf-8")
            start = 0
            while start < len(b):
                end = min(start + safe_max, len(b))
                # backtrack to avoid cutting multibyte char
                while end > start:
                    try:
                        piece = b[start:end].decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        end -= 1
                if end <= start:
                    # can't decode even a single byte (unlikely), fallback with replace
                    piece = b[start:start+safe_max].decode("utf-8", errors="replace")
                    end = start + safe_max
                groups.append([piece.rstrip("\n")])
                start = end
            continue

        if cur_bytes + ln_b <= safe_max:
            cur.append(ln)
            cur_bytes += ln_b
        else:
            groups.append(cur)
            cur = [ln]
            cur_bytes = ln_b
    if cur:
        groups.append(cur)
    return groups

def build_file_section(pr_number: str, fname: str, status: str, preview: str, diff_lines: List[str]) -> List[str]:
    """
    Build one top-level file section. Returns one or more HTML/markdown strings
    (already fenced/wrapped) that represent that file's section. Each returned
    string is intended to be concatenated with other file sections and then
    chunked further by total bytes.
    """
    fname_safe = html.escape(fname)
    # emoji for status
    if status == "A" or status.lower() == "added":
        status_label = "➕ added"
    elif status == "D" or status.lower() == "removed":
        status_label = "➖ removed"
    else:
        status_label = "✏️ modified"

    header = f"<details>\n<summary>{fname_safe} ({status_label})</summary>\n\n"
    header += f"**Preview:**\n\n{html.escape(preview or '(no preview available)')}\n\n"
    header += "**Legend:** `+` = addition (green), `-` = removal (red)\n\n"

    body_parts: List[str] = []
    # if this is an added file and no diff_lines, try to show a short 'Added' message
    if (status == "A" or status.lower() == "added") and (not diff_lines):
        inner = "_(New file — content omitted)_\n"
        body_parts.append(inner)
    elif (status == "D" or status.lower() == "removed") and (not diff_lines):
        inner = "_(Removed file — content omitted)_\n"
        body_parts.append(inner)
    else:
        # We have diff_lines; split into byte-safe fenced chunks (diff)
        if not diff_lines:
            body_parts.append("✅ No meaningful changes detected.\n")
        else:
            groups = chunk_lines_by_bytes(diff_lines, SAFE_COMMENT_SIZE)
            total = len(groups)
            for idx, grp in enumerate(groups, start=1):
                # prepare fenced diff
                fenced = "```diff\n" + "\n".join(grp) + "\n```"
                # each group is placed inside a nested details for compactness
                part = (
                    f"<details>\n<summary>Diff Part {idx}/{total} — click to expand</summary>\n\n"
                    f"{fenced}\n\n</details>\n"
                )
                body_parts.append(part)

    footer = "\n</details>\n\n---\n"
    # combine header + parts + footer into one string (single file section)
    combined = header + "".join(body_parts) + footer
    return [combined]

def split_top_level_bodies(sections: List[str], max_bytes: int) -> List[str]:
    """
    Pack multiple section strings (which may contain nested fenced blocks)
    into comment bodies ensuring each body <= max_bytes bytes.
    This is a simple accumulator (byte-aware): append section to current body
    until adding it would exceed max_bytes, then start a new body.
    """
    bodies = []
    cur = ""
    cur_b = 0
    for s in sections:
        s_b = byte_len(s)
        if cur_b + s_b <= max_bytes:
            cur += s
            cur_b += s_b
        else:
            if cur:
                bodies.append(cur)
            # if single section is larger than max_bytes (rare because we chunked diffs),
            # further split it by lines as a last resort.
            if s_b > max_bytes:
                logging.warning("Single file section exceeds max bytes; splitting by lines as fallback.")
                lines = s.splitlines(keepends=True)
                temp = ""
                temp_b = 0
                for ln in lines:
                    ln_b = byte_len(ln)
                    if temp_b + ln_b > max_bytes:
                        bodies.append(temp)
                        temp = ln
                        temp_b = ln_b
                    else:
                        temp += ln
                        temp_b += ln_b
                if temp:
                    bodies.append(temp)
                cur = ""
                cur_b = 0
            else:
                cur = s
                cur_b = s_b
    if cur:
        bodies.append(cur)
    # add a small tip at the end of last body
    if bodies:
        if byte_len(bodies[-1]) + 200 < max_bytes:
            bodies[-1] += f"\n\n*Tip:* Search for the tag `#tableau-diff-pr {os.getenv('PR_NUMBER','')}` to find these comments."
    return bodies

def make_preview_from_file(path: str, max_lines: int = 6) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            preview_lines = []
            for ln in f:
                if ln.strip():
                    preview_lines.append(ln.strip())
                if len(preview_lines) >= max_lines:
                    break
            return "\n".join(preview_lines)
    except Exception:
        return "(could not extract preview)"

def main():
    base_branch = os.getenv("BASE_BRANCH")
    head_branch = os.getenv("HEAD_BRANCH")
    pr_number   = os.getenv("PR_NUMBER")

    if not base_branch or not head_branch or not pr_number:
        logging.error("Missing BASE_BRANCH, HEAD_BRANCH, or PR_NUMBER in env.")
        sys.exit(1)

    logging.info("Starting local diff bot for PR %s (head=%s base=%s)", pr_number, head_branch, base_branch)

    workdir = tempfile.mkdtemp(prefix="tableau_diff_")
    sections_all: List[str] = []

    # Ensure remote base exists as origin/<base_branch> — Jenkinsfile typically fetches it already,
    # but attempt a fetch (ignore failures)
    try:
        subprocess.check_call(f"git fetch origin +refs/heads/{base_branch}:refs/remotes/origin/{base_branch}", shell=True)
    except Exception:
        logging.debug("Fetching origin/%s failed or not necessary", base_branch)

    # Use git to list changed files between base and head
    cmd = f"git diff --name-status origin/{base_branch}...HEAD"
    try:
        output = subprocess.check_output(cmd, shell=True, text=True)
    except subprocess.CalledProcessError as e:
        logging.error("git diff command failed: %s", e)
        sys.exit(1)

    changed_files = []
    for line in output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        status, fname = parts[0], parts[1]
        if fname.lower().endswith((".twb", ".twbx")):
            changed_files.append((status, fname))

    logging.info("Detected %d Tableau file(s) changed", len(changed_files))

    for status, fname in changed_files:
        logging.info("Processing %s (%s)", fname, status)
        # head file path is local working tree file (relative path)
        file_head_path = extract_file(fname, workdir)
        # prepare base tempfile path
        base_tmp = Path(workdir) / f"base_{Path(fname).name}"
        file_base_path = None

        # Try to extract base version from origin/<base_branch>
        try:
            # Use git show to dump file; this may fail (deleted file / new file)
            subprocess.check_call(f"git show origin/{base_branch}:{fname} > {base_tmp}", shell=True)
            file_base_path = extract_file(str(base_tmp), workdir)
        except Exception:
            logging.debug("Base version not available for %s", fname)

        # If head file is a twbx path in worktree, extract its twb to temp as well for consistent diff
        head_tmp = None
        if file_head_path and file_head_path.lower().endswith(".twbx"):
            head_tmp = Path(workdir) / f"head_{Path(fname).name}"
            try:
                # use same extract_file logic by copying current file to head_tmp then extracting
                subprocess.check_call(f"cp '{fname}' '{head_tmp}'", shell=True)
                file_head_path = extract_file(str(head_tmp), workdir)
            except Exception:
                logging.debug("Could not prepare head extracted file for %s", fname)

        # Build preview from whichever we have
        preview = ""
        if file_head_path:
            preview = make_preview_from_file(file_head_path)
        elif file_base_path:
            preview = make_preview_from_file(file_base_path)

        if file_base_path and file_head_path:
            diff_lines = run_diff(file_base_path, file_head_path)
        elif file_head_path and not file_base_path:
            # added file: read head file content and present it as plus-prefixed lines
            try:
                with open(file_head_path, encoding="utf-8", errors="ignore") as fh:
                    content_lines = [l.rstrip("\n") for l in fh.readlines()]
                diff_lines = ["+" + l for l in content_lines]
            except Exception:
                diff_lines = [f"+ (could not read new file content for {fname})"]
        elif file_base_path and not file_head_path:
            try:
                with open(file_base_path, encoding="utf-8", errors="ignore") as fb:
                    content_lines = [l.rstrip("\n") for l in fb.readlines()]
                diff_lines = ["-" + l for l in content_lines]
            except Exception:
                diff_lines = [f"- (could not read old file content for {fname})"]
        else:
            logging.warning("No head or base content available for %s — skipping", fname)
            continue

        # build one or more HTML/markdown sections for this file
        file_sections = build_file_section(pr_number, fname, status, preview, diff_lines)
        sections_all.extend(file_sections)

    if not sections_all:
        logging.info("No Tableau diffs to report.")
        with open(COMMENT_FILE, "w", encoding="utf-8") as f:
            json.dump([f"#tableau-diff-pr {pr_number}\nNo relevant changes detected."], f)
        return

    # Pack sections into comment bodies <= SAFE_COMMENT_SIZE bytes
    comment_bodies = split_top_level_bodies(sections_all, SAFE_COMMENT_SIZE)

    # final sanity: ensure each body <= SAFE_COMMENT_SIZE; if not, truncate as last resort
    final_bodies = []
    for b in comment_bodies:
        if byte_len(b) <= SAFE_COMMENT_SIZE:
            final_bodies.append(b)
            continue
        logging.warning("A comment body still exceeds SAFE_COMMENT_SIZE; truncating.")
        # safe truncation at byte boundaries while preserving utf-8
        b_bytes = b.encode("utf-8", errors="replace")
        truncated = b_bytes[:SAFE_COMMENT_SIZE - 100].decode("utf-8", errors="ignore")  # leave small tail
        truncated += "\n\n*Truncated due to size.*"
        final_bodies.append(truncated)

    with open(COMMENT_FILE, "w", encoding="utf-8") as f:
        json.dump(final_bodies, f)

    logging.info("Saved %d comment body part(s) into %s", len(final_bodies), COMMENT_FILE)

if __name__ == "__main__":
    main()
