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

    // Posting retry/backoff settings (adjust if you want)
    COMMENT_POST_MAX_ATTEMPTS = '5'
    COMMENT_POST_INITIAL_DELAY_SEC = '2'
    COMMENT_POST_BACKOFF_FACTOR = '2'
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
/bin/bash -euo pipefail
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
'''
      }
    }

    stage('PR: Run Tableau Diff Bot') {
      when {
        expression { return env.CHANGE_ID != null && env.CHANGE_ID != '' }
      }
      steps {
        script {
          echo "PR build detected (PR #${env.CHANGE_ID}) targeting '${env.CHANGE_TARGET}' -> running diff bot (local git diffs)."

          // Only tableau creds are required for deploy stage; not used here but kept if needed later
          withCredentials([
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            sh '''
/bin/bash -euo pipefail
# activate venv if exists
if [ -f .venv/bin/activate ]; then
  . .venv/bin/activate
fi

# Export environment variables that the python bot expects
export OWNER="${OWNER:-$(git config --get remote.origin.url | awk -F'[:/]' '{print $(NF-1)}' | sed -e 's/\\.git$//')}"
export REPO="${REPO:-$(git config --get remote.origin.url | awk -F'[:/]' '{print $NF}' | sed -e 's/\\.git$//')}"
export PR_NUMBER="${CHANGE_ID}"
export HEAD_BRANCH="${CHANGE_BRANCH}"
export BASE_BRANCH="${CHANGE_TARGET}"
export DRY_RUN="${DRY_RUN_DEFAULT}"

echo "Running python diff bot: OWNER=${OWNER} REPO=${REPO} PR=${PR_NUMBER} head=${HEAD_BRANCH} base=${BASE_BRANCH}"
python "${TABLEAU_DIFF_PY}"
'''
          } // withCredentials
        } // script
      } // steps
    } // stage PR

    stage('PR: Post Diff Comments') {
      when {
        expression { return env.CHANGE_ID != null && env.CHANGE_ID != '' }
      }
      steps {
        script {
          // Read comment_bodies.json produced by the python bot and post using pullRequest.comment(...)
          def bodyFile = 'comment_bodies.json'
          def maxAttempts = env.COMMENT_POST_MAX_ATTEMPTS as Integer
          def initialDelay = env.COMMENT_POST_INITIAL_DELAY_SEC as Integer
          def backoff = env.COMMENT_POST_BACKOFF_FACTOR as Integer

          if (!fileExists(bodyFile)) {
            error "Expected ${bodyFile} not found. Bot did not produce comment bodies."
          }

          def content = readFile(file: bodyFile)
          def json = new groovy.json.JsonSlurper().parseText(content)
          if (!(json instanceof List)) {
            error "Malformed ${bodyFile}: expected JSON array of strings."
          }

          if (!binding.hasVariable('pullRequest')) {
            error "No 'pullRequest' object available in this build. Ensure this is a multibranch PR build that provides 'pullRequest'."
          }

          for (int i = 0; i < json.size(); i++) {
            def idx = i + 1
            def total = json.size()
            def body = json[i] as String
            def attempt = 0
            def delay = initialDelay
            def posted = false
            while (attempt < maxAttempts && !posted) {
              try {
                echo "Posting PR comment part ${idx}/${total} (attempt ${attempt+1})..."
                // pullRequest.comment accepts a single String argument (not a map)
                pullRequest.comment(body)
                posted = true
                echo "Posted part ${idx}/${total}"
              } catch (err) {
                attempt++
                echo "❌ Failed to post comment part ${idx} attempt ${attempt}: ${err}"
                if (attempt >= maxAttempts) {
                  error "Failed to post comment part ${idx} after ${attempt} attempts. Aborting."
                } else {
                  echo "Sleeping ${delay}s before retry..."
                  sleep time: delay, unit: 'SECONDS'
                  delay = delay * backoff
                }
              }
            } // attempt loop
            // small delay between parts to avoid rate issues
            sleep time: 1, unit: 'SECONDS'
          } // for parts
        } // script
      } // steps
    } // stage post comments

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
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            sh '''
/bin/bash -euo pipefail
# derive owner/repo from git remote
GIT_URL="$(git config --get remote.origin.url 2>/dev/null || true)"
if [ -z "$GIT_URL" ]; then
  echo "ERROR: cannot determine git remote URL to call GitHub API."
  exit 1
fi
CLEAN_URL="${GIT_URL%.git}"
OWNER_REPO="$(echo "$CLEAN_URL" | awk -F'[:/]' '{print $(NF-1) "/" $NF}')"
OWNER="$(echo "$OWNER_REPO" | cut -d'/' -f1)"
REPO="$(echo "$OWNER_REPO" | cut -d'/' -f2)"

SHA="$(git rev-parse HEAD)"
echo "Querying GitHub for PRs linked to commit $SHA..."

PRS_JSON="$(curl -s -H "Accept: application/vnd.github.groot-preview+json" -H "Authorization: token ${GITHUB_TOKEN:-}" "https://api.github.com/repos/${OWNER}/${REPO}/commits/${SHA}/pulls")"

if [ -z "$PRS_JSON" ] || [ "$PRS_JSON" = "null" ]; then
  echo "ERROR: Empty response from GitHub for commit PR list. Aborting."
  exit 1
fi

PR_NUMBER="$(echo "$PRS_JSON" | jq -r '.[] | select(.head.ref=="prod" and .merged==true) | .number' | head -n1 || true)"
if [ -z "$PR_NUMBER" ] || [ "$PR_NUMBER" = "null" ]; then
  echo "No merged prod->main PR found for commit $SHA. Skipping deployment."
  exit 0
fi

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

