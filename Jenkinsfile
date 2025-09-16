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

                # Derive owner/repo robustly without using sed escapes:
                # handles:
                #   git@github.com:owner/repo.git
                #   https://github.com/owner/repo.git
                #   ssh://git@github.com/owner/repo.git
                GIT_URL="$(git config --get remote.origin.url 2>/dev/null || true)"
                OWNER=""
                REPO=""
                if [ -n "$GIT_URL" ]; then
                  # remove trailing .git using bash parameter expansion (no backslashes)
                  CLEAN_URL="${GIT_URL%.git}"
                  # split on ":" or "/" and take last two tokens using awk
                  OWNER_REPO="$(echo "$CLEAN_URL" | awk -F'[:/]' '{print $(NF-1)\"/\"$NF}')"
                  if [ -n "$OWNER_REPO" ] && [ "$(echo "$OWNER_REPO" | awk -F'/' '{print NF}')" -ge 2 ]; then
                    OWNER="$(echo "$OWNER_REPO" | cut -d'/' -f1)"
                    REPO="$(echo "$OWNER_REPO" | cut -d'/' -f2)"
                  fi
                fi

                # fallback to webhook-provided envs if any (GenericTrigger)
                if [ -z "$OWNER" ] || [ -z "$REPO" ]; then
                  OWNER="${OWNER:-$OWNER_FROM_WEBHOOK}"
                  REPO="${REPO:-$REPO_FROM_WEBHOOK}"
                fi

                echo "Derived OWNER='$OWNER' REPO='$REPO'"

                # Map Jenkins multibranch CHANGE_* envs into variables expected by python bot
                export PR_NUMBER="${CHANGE_ID}"
                export PR_SOURCE_BRANCH="${CHANGE_BRANCH}"
                export PR_TARGET_BRANCH="${CHANGE_TARGET}"
                export OWNER="${OWNER}"
                export REPO="${REPO}"
                export DRY_RUN="${DRY_RUN_DEFAULT}"

                echo "Running diff bot for ${OWNER}/${REPO} PR ${PR_NUMBER} (head=${PR_SOURCE_BRANCH} base=${PR_TARGET_BRANCH})"

                # Run python diff bot (secrets available from withCredentials)
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

                # Derive owner/repo (robust method, no sed backslashes)
                GIT_URL="$(git config --get remote.origin.url 2>/dev/null || true)"
                if [ -z "$GIT_URL" ]; then
                  echo "ERROR: cannot determine git remote URL to call GitHub API."
                  exit 1
                fi
                CLEAN_URL="${GIT_URL%.git}"
                OWNER_REPO="$(echo "$CLEAN_URL" | awk -F"[:/]" "{print \$(NF-1)\"/\"\$NF}")"
                OWNER="$(echo "$OWNER_REPO" | cut -d'/' -f1)"
                REPO="$(echo "$OWNER_REPO" | cut -d'/' -f2)"

                SHA="$(git rev-parse HEAD)"
                echo "Querying GitHub for PRs linked to commit $SHA..."

                PRS_JSON="$(curl -s -H "Accept: application/vnd.github.groot-preview+json" -H "Authorization: token ${GITHUB_TOKEN}" "https://api.github.com/repos/${OWNER}/${REPO}/commits/${SHA}/pulls")"

                if [ -z "$PRS_JSON" ] || [ "$PRS_JSON" = "null" ]; then
                  echo "ERROR: Empty response from GitHub for commit PR list. Aborting."
                  exit 1
                fi

                # find merged PR where head.ref == "prod"
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
