#!/usr/bin/env python3
"""
Tableau diff bot — generate diffs and save as comment chunks in diffs.txt.
Jenkins will later read this file and push comments via pullRequest.comment.
"""
import os
import tempfile
import logging
from pathlib import Path
from typing import List, Dict
import difflib
import zipfile
import re
import time
import base64
import requests
from dotenv import load_dotenv

load_dotenv()

OWNER = os.getenv("OWNER")
REPO = os.getenv("REPO")
PR_NUMBER = os.getenv("PR_NUMBER")
HEAD_BRANCH = os.getenv("HEAD_BRANCH")
BASE_BRANCH = os.getenv("BASE_BRANCH")

SAFE_COMMENT_CHARS = 60000
MAX_LINES_PER_SECTION = 1000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("tableau-diff")

session = requests.Session()
session.headers.update({
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "tableau-diff-bot"
})


def fetch_pr_files(owner: str, repo: str, pr_number: str) -> List[dict]:
    results = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files?page={page}&per_page=100"
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            logger.error(f"Failed to fetch PR files: {r.status_code} {r.text}")
            break
        chunk = r.json()
        if not chunk:
            break
        results.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return results


def _fetch_file(owner: str, repo: str, path: str, ref: str) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    r = session.get(url, timeout=30)
    if r.status_code == 200:
        data = r.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return ""


def normalize_xml_for_diff(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    cleaned = []
    for ln in lines:
        if "last-modified" in ln.lower():
            continue
        cleaned.append(ln)
    return "\n".join(cleaned)


def generate_diff(old: str, new: str) -> List[str]:
    return list(difflib.unified_diff(
        normalize_xml_for_diff(old).splitlines(),
        normalize_xml_for_diff(new).splitlines(),
        fromfile="old", tofile="new", lineterm=""
    ))


def build_section(summary: Dict) -> str:
    fp = summary["file_path"]
    status = summary["status"]
    header = f"### {fp} — {status}\n\n"
    preview = summary.get("preview", "(no preview)")
    body = f"Preview:\n\n{preview}\n\n"

    if status == "added":
        lines = ["+" + l for l in summary.get("content", "").splitlines()]
    elif status == "removed":
        lines = ["-" + l for l in summary.get("content", "").splitlines()]
    else:
        lines = summary.get("diff_lines", [])

    if not lines:
        body += "✅ No meaningful changes\n"
    else:
        total = (len(lines) - 1) // MAX_LINES_PER_SECTION + 1
        for i in range(0, len(lines), MAX_LINES_PER_SECTION):
            chunk = lines[i: i + MAX_LINES_PER_SECTION]
            part = i // MAX_LINES_PER_SECTION + 1
            body += f"<details><summary>Part {part}/{total}</summary>\n\n```diff\n"
            body += "\n".join(chunk)
            body += "\n```\n\n</details>\n"
    return header + body + "\n---\n"


def save_comments_to_file(pr_number: str, sections: List[str], out_path="diffs.txt"):
    header = f"#tableau-diff-pr {pr_number}\n\nAutomated Tableau diff summary for PR {pr_number}.\n\n"
    bodies = []
    current = header
    for sec in sections:
        if len(current.encode("utf-8")) + len(sec.encode("utf-8")) <= SAFE_COMMENT_CHARS:
            current += sec
        else:
            bodies.append(current)
            current = header + sec
    if current.strip():
        bodies.append(current)

    with open(out_path, "w", encoding="utf-8") as f:
        for i, body in enumerate(bodies, start=1):
            f.write(f"---COMMENT_PART_{i}---\n")
            f.write(body)
            f.write("\n---END_COMMENT_PART---\n\n")


def main():
    logger.info(f"Running diff bot for {OWNER}/{REPO} PR {PR_NUMBER}")
    files = fetch_pr_files(OWNER, REPO, PR_NUMBER)
    summaries = []
    for f in files:
        path = f.get("filename")
        status = f.get("status")
        if not path.endswith((".twb", ".twbx")):
            continue
        old, new = "", ""
        if status != "added":
            old = _fetch_file(OWNER, REPO, path, BASE_BRANCH)
        if status != "removed":
            new = _fetch_file(OWNER, REPO, path, HEAD_BRANCH)

        summary = {"file_path": path, "status": status}
        if status == "added":
            summary["content"] = new
        elif status == "removed":
            summary["content"] = old
        else:
            summary["diff_lines"] = generate_diff(old, new)
        summary["preview"] = (new or old).splitlines()[0:5]
        summary["preview"] = "\n".join(summary["preview"])
        summaries.append(summary)

    sections = [build_section(s) for s in summaries]
    save_comments_to_file(PR_NUMBER, sections)
    logger.info(f"Saved {len(sections)} sections into diffs.txt")


if __name__ == "__main__":
    main()
