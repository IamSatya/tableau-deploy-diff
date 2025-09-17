import os
import difflib
import zipfile
import tempfile
import logging
import shutil

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def extract_twb_from_twbx(twbx_path, extract_to):
    with zipfile.ZipFile(twbx_path, 'r') as z:
        for f in z.namelist():
            if f.endswith('.twb'):
                z.extract(f, extract_to)
                return os.path.join(extract_to, f)
    return None

def generate_diff(old_file, new_file):
    with open(old_file, 'r', encoding='utf-8', errors='ignore') as f1, \
         open(new_file, 'r', encoding='utf-8', errors='ignore') as f2:
        old_lines = f1.readlines()
        new_lines = f2.readlines()
    return list(difflib.unified_diff(old_lines, new_lines, fromfile=old_file, tofile=new_file))

def main():
    pr_number = os.getenv("PR_NUMBER")
    head_branch = os.getenv("HEAD_BRANCH")
    base_branch = os.getenv("BASE_BRANCH")

    if not pr_number or not head_branch or not base_branch:
        logging.error("Missing PR context variables")
        return

    logging.info(f"Running diff bot for PR {pr_number}")

    tmpdir = tempfile.mkdtemp()
    output_sections = []

    try:
        workbooks_dir = "workbooks"
        if not os.path.isdir(workbooks_dir):
            logging.warning("No workbooks directory found, skipping")
        else:
            for root, _, files in os.walk(workbooks_dir):
                for f in files:
                    if f.endswith(('.twb', '.twbx')):
                        file_path = os.path.join(root, f)
                        logging.info(f"Checking file {file_path}")

                        extracted_file = None
                        if f.endswith('.twbx'):
                            extracted_file = extract_twb_from_twbx(file_path, tmpdir)
                        else:
                            extracted_file = file_path

                        if not extracted_file:
                            continue

                        diff_lines = generate_diff(extracted_file, extracted_file)  # dummy self-diff for now
                        if diff_lines:
                            section = f"### {f} — modified\n\n```diff\n{''.join(diff_lines)}\n```"
                        else:
                            section = f"### {f} — modified\n\n✅ No meaningful changes"

                        output_sections.append(section)
    finally:
        shutil.rmtree(tmpdir)

    if not output_sections:
        output_sections.append("✅ No Tableau file changes detected.")

    with open("diffs.txt", "w", encoding="utf-8") as out:
        for section in output_sections:
            out.write(section + "\n===SECTION===\n")

    logging.info(f"Saved {len(output_sections)} sections into diffs.txt")

if __name__ == "__main__":
    main()

