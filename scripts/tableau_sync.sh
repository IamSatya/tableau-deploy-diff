#!/bin/bash
# ====================================================================================
# Tableau Cloud Git-Ops Synchronization Script (Git Change-Based) - MAPPED FOLDERS
#
# v13 - Deploy mapped GitHub folders into specific Tableau projects
#       Supports folder names with spaces (uses git diff -z).
# ====================================================================================

set -euo pipefail
> log.txt

# --- Configuration Variables ---
TABLEAU_SERVER="https://prod-apsoutheast-b.online.tableau.com"
TABLEAU_USER=${TABLEAU_USER:-}
TABLEAU_PW=${TABLEAU_PW:-}
TABLEAU_SITE_URL="rakeshnarayanadasu-4e2853de57"

GIT_PROJECTS_DIR="workbooks"
LOGFILE="log.txt"
GIT_CHANGES_FILE="git_file_changes.txt"
GIT_CONTENT_DIFF_FILE="git_filetext_changes.txt"
BACKUP_DIR="${WORKSPACE:-.}/tableau_backups"

# Default to dry run unless overridden by Jenkins
DRY_RUN="${DRY_RUN:-true}"

# --- Ignored Projects ---
IGNORED_PROJECTS=("default" "Samples" "External Assets Default Project")

# --- GitHub folder → Tableau project mappings ---
declare -A REPO_TO_TABLEAU_PROJECT
REPO_TO_TABLEAU_PROJECT["workbooks/finance"]="Victor Wang"
REPO_TO_TABLEAU_PROJECT["workbooks/ITGC/SOX/RevtoCash Control"]="Victor Wang/RevtoCash Control"
REPO_TO_TABLEAU_PROJECT["workbooks/ITGC/SOX/GTI-09 Insurance Control"]="Victor Wang/GTI-09 Insurance Control"

# ====================================================================================
# Helper functions
# ====================================================================================

contains_element() {
  local e match="$1"; shift
  for e; do [[ "$e" == "$match" ]] && return 0; done
  return 1
}

sign_in() {
  AUTH_RESPONSE=$(curl -s -X POST "$TABLEAU_SERVER/api/3.22/auth/signin" \
    -H "Content-Type: application/json" -H "Accept: application/json" \
    -d "{\"credentials\": {\"name\": \"$TABLEAU_USER\", \"password\": \"$TABLEAU_PW\", \"site\": {\"contentUrl\": \"$TABLEAU_SITE_URL\"}}}")

  if echo "$AUTH_RESPONSE" | jq -e '.error' >/dev/null 2>&1; then
    echo "FATAL: Authentication failed." | tee -a "$LOGFILE"
    echo "Response: $(echo "$AUTH_RESPONSE" | jq .)" | tee -a "$LOGFILE"
    exit 1
  fi

  TOKEN=$(echo "$AUTH_RESPONSE" | jq -r '.credentials.token')
  SITE_ID=$(echo "$AUTH_RESPONSE" | jq -r '.credentials.site.id')
}

publish_workbook() {
  local file_to_publish="$1"
  local project_name="$2"

  local filename=$(basename "$file_to_publish")
  local workbook="${filename%.*}"
  local ext="${filename##*.}"

  # Ensure project exists
  if [[ -z "${tableau_projects[$project_name]+_}" ]]; then
    echo "Creating project '$project_name'..." | tee -a "$LOGFILE"
    if [[ "$DRY_RUN" == "true" ]]; then
      tableau_projects["$project_name"]="DRY_RUN_PROJECT"
    else
      CREATE_RESP=$(curl -s -X POST "$TABLEAU_SERVER/api/3.22/sites/$SITE_ID/projects" \
        -H "Content-Type: application/json" -H "X-Tableau-Auth: $TOKEN" \
        -d "{\"project\": {\"name\": \"$project_name\"}}")
      tableau_projects["$project_name"]=$(echo "$CREATE_RESP" | jq -r '.project.id')
    fi
  fi

  local project_id=${tableau_projects[$project_name]}
  local boundary="------------------------$(openssl rand -hex 16)"

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "DRY_RUN: Would publish $filename to $project_name" | tee -a "$LOGFILE"
    return 0
  fi

  ( printf -- "--%s\r\n" "$boundary"
    printf "Content-Disposition: name=\"request_payload\"\r\n\r\n<tsRequest><workbook name=\"%s\" showTabs=\"false\"><project id=\"%s\"/></workbook></tsRequest>\r\n" "$workbook" "$project_id"
    printf -- "--%s\r\n" "$boundary"
    printf "Content-Disposition: name=\"tableau_workbook\"; filename=\"%s\"\r\n\r\n" "$filename"
    cat "$file_to_publish"
    printf "\r\n--%s--\r\n" "$boundary"
  ) > publish_body.bin

  RESP=$(curl -s -X POST "$TABLEAU_SERVER/api/3.22/sites/$SITE_ID/workbooks?workbookType=$ext&overwrite=true" \
    -H "Content-Type: multipart/mixed; boundary=$boundary" -H "X-Tableau-Auth: $TOKEN" \
    --data-binary @publish_body.bin)
  rm publish_body.bin

  if echo "$RESP" | jq -e '.error' >/dev/null 2>&1; then
    echo "ERROR: Failed to publish $filename" | tee -a "$LOGFILE"
    echo "Response: $RESP" | tee -a "$LOGFILE"
    return 1
  fi
  echo "SUCCESS: Published $filename to $project_name" | tee -a "$LOGFILE"
}

# ====================================================================================
# Script body
# ====================================================================================

echo "=== Tableau Cloud Sync (mapped folders) ===" | tee -a "$LOGFILE"
echo "DRY_RUN=$DRY_RUN" | tee -a "$LOGFILE"
date | tee -a "$LOGFILE"

sign_in
echo "Auth successful." | tee -a "$LOGFILE"

# Collect existing projects/workbooks
LIST_PROJECTS=$(curl -s -X GET "$TABLEAU_SERVER/api/3.22/sites/$SITE_ID/projects?pageSize=1000" -H "X-Tableau-Auth: $TOKEN")
declare -A tableau_projects
while IFS=$'\t' read -r name id; do
  [[ -n "$name" ]] && tableau_projects["$name"]="$id"
done < <(echo "$LIST_PROJECTS" | jq -r '.projects.project[] | "\(.name)\t\(.id)"')

LIST_WBS=$(curl -s -X GET "$TABLEAU_SERVER/api/3.22/sites/$SITE_ID/workbooks?pageSize=1000" -H "X-Tableau-Auth: $TOKEN")
declare -A tableau_workbooks
while IFS=$'\t' read -r key id; do
  [[ -n "$key" ]] && tableau_workbooks["$key"]="$id"
done < <(echo "$LIST_WBS" | jq -r '.workbooks.workbook[] | "\(.project.name)/\(.name)\t\(.id)"')

# Detect changes (NUL-safe)
git -c core.quotePath=false diff --name-status -z HEAD^1 HEAD > "$GIT_CHANGES_FILE"

if [ ! -s "$GIT_CHANGES_FILE" ]; then
  echo "No changes detected." | tee -a "$LOGFILE"
  exit 0
fi

while IFS= read -r -d '' entry; do
  status="${entry%%$'\t'*}"
  rest="${entry#*$'\t'}"

  if [[ "$status" == R* ]]; then
    path1="${rest%%$'\t'*}"
    path2="${rest#*$'\t'}"
  else
    path1="$rest"
    path2=""
  fi

  current="${path2:-$path1}"

  # Only .twb/.twbx under mapped dirs
  if ! [[ "$current" == "$GIT_PROJECTS_DIR/"* && ( "$current" == *.twb || "$current" == *.twbx ) ]]; then
    continue
  fi

  # Match mapping
  match=""
  for k in "${!REPO_TO_TABLEAU_PROJECT[@]}"; do
    if [[ "$current" == "$k/"* ]]; then
      [[ -z "$match" || ${#k} -gt ${#match} ]] && match="$k"
    fi
  done
  [[ -z "$match" ]] && continue

  target="${REPO_TO_TABLEAU_PROJECT[$match]}"
  fname=$(basename "$current")
  wname="${fname%.*}"

  case ${status:0:1} in
    A|M)
      echo "Publishing $current -> $target" | tee -a "$LOGFILE"
      publish_workbook "$current" "$target"
      ;;
    D)
      key="$target/$wname"
      if [[ -n "${tableau_workbooks[$key]+_}" ]]; then
        wid=${tableau_workbooks[$key]}
        echo "Deleting workbook $key (id=$wid)" | tee -a "$LOGFILE"
        [[ "$DRY_RUN" == "true" ]] || curl -s -X DELETE "$TABLEAU_SERVER/api/3.22/sites/$SITE_ID/workbooks/$wid" -H "X-Tableau-Auth: $TOKEN"
      fi
      ;;
    R)
      old="$path1"; new="$path2"
      old_name="${old##*/}"; old_name="${old_name%.*}"
      old_key="$target/$old_name"
      if [[ -n "${tableau_workbooks[$old_key]+_}" ]]; then
        wid=${tableau_workbooks[$old_key]}
        echo "Deleting old workbook $old_key (id=$wid)" | tee -a "$LOGFILE"
        [[ "$DRY_RUN" == "true" ]] || curl -s -X DELETE "$TABLEAU_SERVER/api/3.22/sites/$SITE_ID/workbooks/$wid" -H "X-Tableau-Auth: $TOKEN"
      fi
      echo "Publishing renamed workbook $new -> $target" | tee -a "$LOGFILE"
      publish_workbook "$new" "$target"
      ;;
  esac
done < "$GIT_CHANGES_FILE"

# Sign out
[[ "$DRY_RUN" == "true" ]] || curl -s -X POST "$TABLEAU_SERVER/api/3.22/auth/signout" -H "X-Tableau-Auth: $TOKEN" >/dev/null
echo "Sync complete." | tee -a "$LOGFILE"
