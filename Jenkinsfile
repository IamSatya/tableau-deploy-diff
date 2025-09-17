pipeline {
  agent any

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
/bin/bash -e <<'BASH'
set -euo pipefail

if [ ! -d .venv ]; then
  python3 -m venv .venv || python -m venv .venv
fi
. .venv/bin/activate

pip install --upgrade pip || true
pip install requests python-dotenv jq || true
BASH
'''
      }
    }

    stage('PR: Run Tableau Diff Bot') {
      when {
        expression { return env.CHANGE_ID != null && env.CHANGE_ID != '' }
      }
      steps {
        script {
          echo "PR build detected (PR #${env.CHANGE_ID}) targeting '${env.CHANGE_TARGET}' -> running diff bot."

          withCredentials([
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            sh '''
/bin/bash -e <<'BASH'
set -euo pipefail

if [ -f .venv/bin/activate ]; then
  . .venv/bin/activate
fi

export PR_NUMBER="${CHANGE_ID}"
export HEAD_BRANCH="${CHANGE_BRANCH}"
export BASE_BRANCH="${CHANGE_TARGET}"
export DRY_RUN="${DRY_RUN_DEFAULT}"

python "${TABLEAU_DIFF_PY}"
BASH
'''
          }
        }
      }
    }

    stage('PR: Post Diff Comments') {
      when {
        expression { return env.CHANGE_ID != null && env.CHANGE_ID != '' }
      }
      steps {
        script {
          def sections = readFile('diffs.txt').split('===SECTION===')
          int idx = 1
          for (section in sections) {
            def trimmed = section.trim()
            if (trimmed) {
              retry(3) {
                try {
                  echo "📢 Posting diff section ${idx} to PR #${env.CHANGE_ID}"
                  pullRequest.comment(trimmed)   // ✅ only plugin-based comment
                } catch (err) {
                  echo "❌ Failed to post comment part ${idx}, retrying: ${err}"
                  sleep 5
                  throw err
                }
              }
              idx++
            }
          }
        }
      }
    }

    stage('Deploy to Tableau (main - automatic on prod->main PR merge)') {
      when {
        allOf {
          expression { return env.BRANCH_NAME == 'main' }
          expression { return env.CHANGE_ID == null || env.CHANGE_ID == '' }
        }
      }
      steps {
        script {
          echo "Main branch build detected. Deploying Tableau dashboards..."
          withCredentials([
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            sh '''
/bin/bash -e <<'BASH'
set -euo pipefail

if [ -f "${TABLEAU_SYNC_SCRIPT}" ]; then
  chmod +x "${TABLEAU_SYNC_SCRIPT}" || true
  export TABLEAU_USER="${TABLEAU_USER}"
  export TABLEAU_PW="${TABLEAU_PW}"
  export DRY_RUN="false"
  "${TABLEAU_SYNC_SCRIPT}"
else
  echo "ERROR: ${TABLEAU_SYNC_SCRIPT} not found!"
  exit 1
fi
BASH
'''
          }
        }
      }
    }
  }

  post {
    success {
      echo "✅ Pipeline succeeded for branch ${env.BRANCH_NAME} (CHANGE_ID=${env.CHANGE_ID})"
    }
    failure {
      echo "❌ Pipeline failed for branch ${env.BRANCH_NAME} (CHANGE_ID=${env.CHANGE_ID})"
    }
  }
}

