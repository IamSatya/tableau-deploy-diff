import os, zipfile, difflib, tempfile, requests, traceback
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_API = os.getenv("GITHUB_API", "https://api.github.com")
BOT_USERNAME = os.getenv("BOT_USERNAME", "tableau-diff-bot")
MAX_LINES_PER_COMMENT = 1000


def extract_twb_content(path, original_name):
    if original_name.endswith(".twb"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"[extract] Error reading .twb: {e}")
            traceback.print_exc()
    elif original_name.endswith(".twbx"):
        try:
            with zipfile.ZipFile(path, "r") as z:
                twb_files = [f for f in z.namelist() if f.endswith(".twb")]
                if not twb_files:
                    print(f"[extract] No .twb file found inside {path}")
                    return ""
                with z.open(twb_files[0]) as twb_file:
                    return twb_file.read().decode("utf-8")
        except Exception as e:
            print(f"[extract] Error with .twbx: {e}")
            traceback.print_exc()
    return ""


def generate_minimal_diff(old_content, new_content):
    diff = difflib.unified_diff(
        old_content.splitlines(),
        new_content.splitlines(),
        fromfile="old.twb",
        tofile="new.twb",
        lineterm="",
    )
    return [line for line in diff if line.startswith(("+", "-", "@@"))]


def post_github_comment(owner, repo, pr_number, body):
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    response = requests.post(url, headers=headers, json={"body": body})
    print(f"📩 Posted comment: {response.status_code} {response.text}")


def paginate_and_post_comment(owner, repo, pr_number, file_path, content, label="xml"):
    lines = content.splitlines()
    for i in range(0, len(lines), MAX_LINES_PER_COMMENT):
        chunk = lines[i : i + MAX_LINES_PER_COMMENT]
        part = i // MAX_LINES_PER_COMMENT + 1
        total = (len(lines) - 1) // MAX_LINES_PER_COMMENT + 1
        comment = (
            f"📄 `{file_path}` (Part {part}/{total})\n\n"
            f"```{label}\n" + "\n".join(chunk) + "\n```"
        )
        post_github_comment(owner, repo, pr_number, comment)


def process_pull_request(owner, repo, pr_number, base_branch, head_branch):
    try:
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}

        files_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        print(f"🔎 Fetching PR files: {files_url}")
        r_files = requests.get(files_url, headers=headers)

        if r_files.status_code != 200:
            print(f"❌ Failed to fetch files: {r_files.status_code} {r_files.text}")
            return

        files = r_files.json()
        if not isinstance(files, list):
            print(f"❌ Unexpected response for files: {files}")
            return

        for change in files:
            file_path = change.get("filename")
            status = change.get("status")
            if not file_path or not status:
                print(f"⚠️ Skipping unexpected file entry: {change}")
                continue

            if not (file_path.endswith(".twb") or file_path.endswith(".twbx")):
                continue

            old_xml, new_xml = "", ""

            with tempfile.TemporaryDirectory() as tmpdir:
                old_path = os.path.join(tmpdir, "old_file")
                new_path = os.path.join(tmpdir, "new_file")

                # Get old file from base branch
                if status != "added":
                    old_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{file_path}?ref={base_branch}"
                    r_old = requests.get(old_url, headers=headers)
                    if r_old.status_code == 200:
                        import base64

                        content = base64.b64decode(r_old.json()["content"])
                        with open(old_path, "wb") as f:
                            f.write(content)
                        old_xml = extract_twb_content(old_path, file_path)
                    else:
                        print(f"⚠️ Failed to fetch old file {file_path}: {r_old.status_code}")

                # Get new file from head branch
                if status != "removed":
                    new_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{file_path}?ref={head_branch}"
                    r_new = requests.get(new_url, headers=headers)
                    if r_new.status_code == 200:
                        import base64

                        content = base64.b64decode(r_new.json()["content"])
                        with open(new_path, "wb") as f:
                            f.write(content)
                        new_xml = extract_twb_content(new_path, file_path)
                    else:
                        print(f"⚠️ Failed to fetch new file {file_path}: {r_new.status_code}")

            # Handle file statuses
            if status == "added" and new_xml:
                paginate_and_post_comment(owner, repo, pr_number, file_path, new_xml, label="xml")
                continue

            if status == "removed" and old_xml:
                paginate_and_post_comment(owner, repo, pr_number, file_path, old_xml, label="xml")
                continue

            if old_xml and new_xml:
                diff_lines = generate_minimal_diff(old_xml, new_xml)
                if not diff_lines:
                    post_github_comment(owner, repo, pr_number, f"✅ No changes in `{file_path}`")
                    continue

                for i in range(0, len(diff_lines), MAX_LINES_PER_COMMENT):
                    chunk = diff_lines[i : i + MAX_LINES_PER_COMMENT]
                    part = i // MAX_LINES_PER_COMMENT + 1
                    total = (len(diff_lines) - 1) // MAX_LINES_PER_COMMENT + 1
                    comment = (
                        f"🔁 Diff for `{file_path}` (Part {part}/{total})\n\n"
                        f"```diff\n" + "\n".join(chunk) + "\n```"
                    )
                    post_github_comment(owner, repo, pr_number, comment)
            else:
                post_github_comment(owner, repo, pr_number, f"⚠️ Could not extract content from `{file_path}`")

    except Exception as e:
        print(f"❌ Error in process_pull_request: {e}")
        traceback.print_exc()


def main():
    """
    CI/CD should pass PR payload as env vars:
      - OWNER
      - REPO
      - PR_NUMBER
      - HEAD_BRANCH
      - BASE_BRANCH
    """
    owner = os.getenv("OWNER")
    repo = os.getenv("REPO")
    pr_number = os.getenv("PR_NUMBER")
    head_branch = os.getenv("HEAD_BRANCH")
    base_branch = os.getenv("BASE_BRANCH")

    print(f"🚀 Starting Tableau Diff Bot with:")
    print(f"   OWNER={owner}, REPO={repo}, PR={pr_number}, HEAD={head_branch}, BASE={base_branch}")

    if not all([owner, repo, pr_number, head_branch, base_branch]):
        print("❌ Missing required environment variables")
        return

    process_pull_request(owner, repo, pr_number, base_branch, head_branch)


if __name__ == "__main__":
    main()
