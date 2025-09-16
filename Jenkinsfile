// Jenkinsfile - multibranch-friendly (patched robust owner/repo parsing)
pipeline {
  agent any

  triggers {
    GenericTrigger(
      genericVariables: [
        [key: 'ACTION',      value: '$.action'],
        [key: 'PR_NUMBER',   value: '$.pull_request.number'],
        [key: 'HEAD_BRANCH', value: '$.pull_request.head.ref'],
        [key: 'BASE_BRANCH', value: '$.pull_request.base.ref'],
        [key: 'MERGED',      value: '$.pull_request.merged']
      ],
      causeString: 'Triggered by GitHub Pull Request Webhook',
      token: 'my-secret-webhook-token',
      printContributedVariables: false,
      printPostContent: false,
      silentResponse: true
    )
  }

  options {
    disableConcurrentBuilds()
    timestamps()
    ansiColor('xterm')
  }

  environment {
    TABLEAU_SYNC_SCRIPT = 'scripts/tableau_sync.sh'
    TABLEAU_DIFF_PY     = 'tableau_diff_bot.py'
    DRY_RUN_DEFAULT     = 'true'
  }

  stages {
    stage('Checkout') {
      steps {
        echo "Branch: ${env.BRANCH_NAME}  CHANGE_ID=${env.CHANGE_ID} CHANGE_TARGET=${env.CHANGE_TARGET} CHANGE_BRANCH=${env.CHANGE_BRANCH}"
        checkout scm
      }
    }

    stage('Prepare Environment') {
      steps {
        sh '''
          bash -lc '
            set -euo pipefail
            if [ ! -d .venv ]; then
              python3 -m venv .venv || python -m venv .venv
            fi
            . .venv/bin/activate
            pip install --upgrade pip || true
            if [ -f requirements.txt ]; then
              pip install -r requirements.txt || true
            else
              pip install requests python-dotenv jq || true
            fi
          '
        '''
      }
    }

    stage('PR: Run Tableau Diff Bot') {
      when {
        expression { return env.CHANGE_ID != null && env.CHANGE_ID != '' }
      }
      steps {
        script {
          echo "PR build detected (PR #${env.CHANGE_ID}) targeting '${env.CHANGE_TARGET}' -> running diff bot (dry-run)."

          withCredentials([
            usernamePassword(credentialsId: 'github-token', usernameVariable: 'GITHUB_USER', passwordVariable: 'GITHUB_TOKEN'),
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            sh '''
              bash -lc '
                set -euo pipefail

                # activate venv if exists
                if [ -f .venv/bin/activate ]; then
                  . .venv/bin/activate
                fi

                # Robust owner/repo derivation:
                # - handle git@github.com:owner/repo.git
                # - handle https://github.com/owner/repo.git
                # - handle ssh://git@github.com/owner/repo.git
                # We transform separators ":" and "/" to spaces and pick the last two tokens.
                GIT_URL="$(git config --get remote.origin.url || true)"
                OWNER=""
                REPO=""
                if [ -n "$GIT_URL" ]; then
                  # remove trailing .git if present
                  GIT_URL="${GIT_URL%.git}"
                  # replace ':' and '/' with space, then take last two fields
                  # use 'set --' to populate positional params robustly
                  IFS=' ' read -r -a parts <<< "$(echo "$GIT_URL" | tr '/:' ' ')"
                  len=${#parts[@]}
                  if [ "$len" -ge 2 ]; then
                    OWNER="${parts[$((len-2))]}"
                    REPO="${parts[$((len-1))]}"
                  fi
                fi

                if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
                  echo "WARNING: Could not derive OWNER/REPO from git remote; falling back to webhook-provided envs if any"
                  # if webhook provided OWNER/REPO envs (GenericTrigger), respect them
                  OWNER="${OWNER:-$OWNER_FROM_WEBHOOK}"
                  REPO="${REPO:-$REPO_FROM_WEBHOOK}"
                fi

                echo "Derived OWNER='$OWNER' REPO='$REPO' from git remote."

                # Map Jenkins multibranch CHANGE_* envs into variables expected by python bot
                export PR_NUMBER="${CHANGE_ID}"
                export PR_SOURCE_BRANCH="${CHANGE_BRANCH}"
                export PR_TARGET_BRANCH="${CHANGE_TARGET}"
                export OWNER="${OWNER}"
                export REPO="${REPO}"
                export DRY_RUN="${DRY_RUN_DEFAULT}"

                echo "Running diff bot for ${OWNER}/${REPO} PR ${PR_NUMBER} (head=${PR_SOURCE_BRANCH} base=${PR_TARGET_BRANCH})"

                # Call python diff bot (secrets available in env via withCredentials)
                python "${TABLEAU_DIFF_PY}"
              '
            '''
          } // withCredentials
        } // script
      } // steps
    } // stage PR

    stage('Deploy to Tableau (main - automatic on prod->main PR merge)') {
      when {
        allOf {
          expression { return env.BRANCH_NAME == 'main' }
          expression { return env.CHANGE_ID == null || env.CHANGE_ID == '' }
        }
      }
      steps {
        script {
          echo "Main branch build detected. Checking for associated merged prod->main PR..."

          withCredentials([
            usernamePassword(credentialsId: 'github-token', usernameVariable: 'GITHUB_USER', passwordVariable: 'GITHUB_TOKEN'),
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            sh '''
              bash -lc '
                set -euo pipefail

                # Derive owner/repo (same robust method)
                GIT_URL="$(git config --get remote.origin.url || true)"
                GIT_URL="${GIT_URL%.git}"
                IFS=" " read -r -a parts <<< "$(echo "$GIT_URL" | tr "/:" " ")"
                len=${#parts[@]}
                if [ "$len" -ge 2 ]; then
                  OWNER="${parts[$((len-2))]}"
                  REPO="${parts[$((len-1))]}"
                else
                  echo "ERROR: Unable to determine owner/repo from git remote."
                  exit 1
                fi

                SHA="$(git rev-parse HEAD)"
                echo "Querying GitHub for PRs linked to commit $SHA..."

                PRS_JSON="$(curl -s -H "Accept: application/vnd.github.groot-preview+json" -H "Authorization: token ${GITHUB_TOKEN}" "https://api.github.com/repos/${OWNER}/${REPO}/commits/${SHA}/pulls")"
                if [ -z "$PRS_JSON" ] || [ "$PRS_JSON" = "null" ]; then
                  echo "ERROR: Empty response from GitHub for commit PR list. Aborting."
                  exit 1
                fi

                PR_NUMBER="$(echo "$PRS_JSON" | jq -r '.[] | select(.head.ref=="prod" and .merged==true) | .number' | head -n1 || true)"
                PR_TITLE="$(echo "$PRS_JSON" | jq -r '.[] | select(.head.ref=="prod" and .merged==true) | .title' | head -n1 || true)"
                PR_USER="$(echo "$PRS_JSON" | jq -r '.[] | select(.head.ref=="prod" and .merged==true) | .user.login' | head -n1 || true)"

                if [ -z "$PR_NUMBER" ] || [ "$PR_NUMBER" = "null" ]; then
                  echo "No merged prod->main PR found for commit $SHA. Skipping deployment."
                  exit 0
                fi

                echo "Found merged PR #${PR_NUMBER} (title: ${PR_TITLE}, author: ${PR_USER}). Proceeding to deploy."

                if [ -f "${TABLEAU_SYNC_SCRIPT}" ]; then
                  chmod +x "${TABLEAU_SYNC_SCRIPT}" || true
                  export TABLEAU_USER="${TABLEAU_USER}"
                  export TABLEAU_PW="${TABLEAU_PW}"
                  export DRY_RUN="false"
                  echo "Invoking ${TABLEAU_SYNC_SCRIPT}..."
                  "${TABLEAU_SYNC_SCRIPT}"
                else
                  echo "ERROR: ${TABLEAU_SYNC_SCRIPT} not found in workspace. Aborting."
                  exit 1
                fi
              '
            '''
          } // withCredentials
        } // script
      } // steps
    } // stage Deploy
  } // stages

  post {
    success {
      echo "✅ Pipeline succeeded for branch ${env.BRANCH_NAME} (CHANGE_ID=${env.CHANGE_ID})"
    }
    failure {
      echo "❌ Pipeline failed for branch ${env.BRANCH_NAME} (CHANGE_ID=${env.CHANGE_ID})"
    }
  }
}
