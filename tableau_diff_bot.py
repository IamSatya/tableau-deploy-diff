# ... all your existing imports + code above remain unchanged ...

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

        # Build header + intro
        header_tag = f"{SEARCHABLE_PR_TAG} {pr_number}"
        intro = (
            f"Automated Tableau diff summary for PR {pr_number}.\n\n"
            "This comment is managed by Jenkins bot.\n\n"
        )

        # Build per-file sections
        file_sections = []
        for s in file_summaries:
            section = build_file_section(s, pr_number)
            file_sections.append(section)

        # Pack into multiple comment bodies
        comment_bodies = pack_sections_to_comment_bodies(header_tag, intro, file_sections, SAFE_COMMENT_CHARS)

        # ✅ Write comment bodies to diffs.txt for Jenkins to post
        with open("diffs.txt", "w", encoding="utf-8") as f:
            for i, body in enumerate(comment_bodies, start=1):
                f.write(body)
                f.write("\n")
                f.write(f"---COMMENT_PART_{i}---\n")
        logger.info(f"Saved {len(comment_bodies)} sections into diffs.txt")

    except Exception:
        logger.exception("Error in process_pull_request")


def main():
    owner = os.getenv("OWNER")
    repo = os.getenv("REPO")
    pr_number = os.getenv("PR_NUMBER")
    head_branch = os.getenv("HEAD_BRANCH")
    base_branch = os.getenv("BASE_BRANCH")

    logger.info(f"Running diff bot for {owner}/{repo} PR {pr_number}")

    if not all([owner, repo, pr_number, head_branch, base_branch]):
        logger.error("Missing required environment variables: OWNER, REPO, PR_NUMBER, HEAD_BRANCH, BASE_BRANCH")
        return

    process_pull_request(owner, repo, pr_number, base_branch, head_branch)


if __name__ == "__main__":
    main()

