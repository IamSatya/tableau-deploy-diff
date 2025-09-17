#!/usr/bin/env python3
import os
import sys
import json
import difflib
import zipfile
import tempfile
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

COMMENT_FILE = os.getenv("SAFE_COMMENT_JSON", "comment_bodies.json")
MAX_COMMENT_SIZE = 60000  # safe upper bound for Jenkins + GitHub comment body

def extract_file(path, workdir):
    """
    If .twbx, extract the embedded .twb file to a temp dir.
    Otherwise, return the file path unchanged.
    """
    if path.endswith(".twbx"):
        with zipfile.ZipFile(path, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".twb"):
                    extracted = Path(workdir) / Path(name).name
                    with zf.open(name) as src, open(extracted, "wb") as dst:
                        dst.write(src.read())
                    logging.info("[extract] Extracted %s -> %s", name, extracted)
                    return str(extracted)
        return None
    return path

def run_diff(file_a, file_b):
    """
    Generate a unified diff between two files.
    Returns diff text as a string.
    """
    try:
        with open(file_a, encoding="utf-8", errors="ignore") as f1, \
             open(file_b, encoding="utf-8", errors="ignore") as f2:
            a_lines = f1.readlines()
            b_lines = f2.readlines()
    except Exception as e:
        logging.error("Failed to read files %s vs %s: %s", file_a, file_b, e)
        return ""

    diff = difflib.unified_diff(
        a_lines, b_lines,
        fromfile=file_a, tofile=file_b,
        lineterm=""
    )
    return "\n".join(diff)

def chunk_text(text, max_size=MAX_COMMENT_SIZE):
    """
    Split text into chunks that are <= max_size.
    """
    if len(text) <= max_size:
        return [text]
    chunks = []
    current = []
    size = 0
    for line in text.splitlines(True):
        if size + len(line) > max_size:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks

def main():
    base_branch = os.getenv("BASE_BRANCH")
    head_branch = os.getenv("HEAD_BRANCH")
    pr_number   = os.getenv("PR_NUMBER")

    if not base_branch or not head_branch or not pr_number:
        logging.error("Missing BASE_BRANCH, HEAD_BRANCH, or PR_NUMBER in env.")
        sys.exit(1)

    logging.info("Starting local diff bot for PR %s (head=%s base=%s)",
                 pr_number, head_branch, base_branch)

    workdir = tempfile.mkdtemp(prefix="tableau_diff_")
    diff_results = []

    # Use git to list changed files between base and head
    cmd = f"git diff --name-status origin/{base_branch}...HEAD"
    import subprocess
    try:
        output = subprocess.check_output(cmd, shell=True, text=True)
    except subprocess.CalledProcessError as e:
        logging.error("git diff command failed: %s", e)
        sys.exit(1)

    changed_files = []
    for line in output.strip().splitlines():
        status, fname = line.split(maxsplit=1)
        if fname.endswith((".twb", ".twbx")):
            changed_files.append((status, fname))

    logging.info("Detected %d Tableau file(s) changed", len(changed_files))

    for status, fname in changed_files:
        file_head = extract_file(fname, workdir)
        file_base = None

        # Try to checkout base version into a temp file
        base_tmp = Path(workdir) / f"base_{Path(fname).name}"
        try:
            subprocess.check_call(
                f"git show origin/{base_branch}:{fname} > {base_tmp}",
                shell=True
            )
            file_base = extract_file(str(base_tmp), workdir)
        except Exception:
            logging.warning("Base version not available for %s", fname)

        if file_head and file_base:
            diff_text = run_diff(file_base, file_head)
        elif file_head:
            diff_text = f"Added new file {fname}\n"
        else:
            diff_text = f"Removed file {fname}\n"

        if not diff_text.strip():
            continue

        header = f"#tableau-diff-pr {pr_number}\n" \
                 f"**File:** `{fname}`\n\n" \
                 "```diff\n" + diff_text + "\n```"

        diff_results.append(header)

    if not diff_results:
        logging.info("No Tableau diffs to report.")
        with open(COMMENT_FILE, "w") as f:
            json.dump(["#tableau-diff-pr %s\nNo relevant changes detected." % pr_number], f)
        return

    # Flatten diffs into one big string, then chunk
    full_text = "\n\n---\n\n".join(diff_results)
    comment_bodies = chunk_text(full_text)

    with open(COMMENT_FILE, "w") as f:
        json.dump(comment_bodies, f)

    logging.info("Saved %d comment body part(s) into %s",
                 len(comment_bodies), COMMENT_FILE)

if __name__ == "__main__":
    main()

