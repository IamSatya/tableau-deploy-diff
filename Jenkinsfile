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
    SAFE_COMMENT_JSON   = 'comment_bodies.json'
    # Backoff settings for posting comments
    POST_RETRY_COUNT    = '5'
    POST_RETRY_DELAY    = '5'
  }

  stages {
    stage('Checkout') {
      steps {
        echo "Branch: ${env.BRANCH_NAME}  CHANGE_ID=${env.CHANGE_ID} CHANGE_TARGET=${env.CHANGE_TARGET} CHANGE_BRANCH=${env.CHANGE_BRANCH}"
        // standard multibranch checkout
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

          // We don't need a GitHub API token anymore for posting — Python will create comment_bodies.json
          withCredentials([
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            sh '''
/bin/bash -euo pipefail

# activate venv if exists
if [ -f .venv/bin/activate ]; then
  . .venv/bin/activate
fi

# Derive owner/repo robustly (no sed backslash escapes)
GIT_URL="$(git config --get remote.origin.url 2>/dev/null || true)"
OWNER=""
REPO=""

if [ -n "$GIT_URL" ]; then
  CLEAN_URL="${GIT_URL%.git}"
  OWNER_REPO="$(echo "$CLEAN_URL" | awk -F'[:/]' '{print $(NF-1) "/" $NF}')"
  if [ -n "$OWNER_REPO" ]; then
    OWNER="$(echo "$OWNER_REPO" | cut -d'/' -f1)"
    REPO="$(echo "$OWNER_REPO" | cut -d'/' -f2)"
  fi
fi

OWNER="${OWNER:-$OWNER_FROM_WEBHOOK}"
REPO="${REPO:-$REPO_FROM_WEBHOOK}"

export OWNER="${OWNER}"
export REPO="${REPO}"
export PR_NUMBER="${CHANGE_ID}"
export HEAD_BRANCH="${CHANGE_BRANCH}"
export BASE_BRANCH="${CHANGE_TARGET}"
export DRY_RUN="${DRY_RUN_DEFAULT}"

echo "Running python diff bot: OWNER=${OWNER} REPO=${REPO} PR=${PR_NUMBER} head=${HEAD_BRANCH} base=${BASE_BRANCH}"

# ensure origin/<base> exists: fetch the base branch from origin
if [ -n "${BASE_BRANCH}" ]; then
  # fetch the base branch explicitly (works for PR builds where origin/<base> may not be present)
  git fetch origin +refs/heads/${BASE_BRANCH}:refs/remotes/origin/${BASE_BRANCH} || true
fi

# run python bot; it will write comment_bodies.json in workspace
python "${TABLEAU_DIFF_PY}"
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
          // read JSON file produced by python bot and post using pullRequest.comment(String)
          def file = env.SAFE_COMMENT_JSON
          if (!fileExists(file)) {
            error "Expected ${file} not found. Bot did not produce comment bodies."
          }

          def jsonText = readFile(file).trim()
          def bodies = []
          try {
            bodies = readJSON text: jsonText
          } catch (err) {
            echo "Failed to parse ${file}: ${err}"
            error "Invalid ${file}"
          }

          if (!(bodies instanceof List)) {
            error "${file} must be a JSON array of strings"
          }

          // Post each string using pullRequest.comment(String)
          def total = bodies.size()
          echo "Will post ${total} comment parts using pullRequest.comment(String)"
          for (int i = 0; i < bodies.size(); i++) {
            def body = bodies[i] as String
            def partIndex = i + 1
            def attempt = 0
            def maxAttempts = env.POST_RETRY_COUNT as Integer
            def posted = false
            while (!posted && attempt < maxAttempts) {
              try {
                // replace header tag with a part-specific header if present
                // (the python bot typically sets a SEARCHABLE_PR_TAG at top of bodies)
                def finalBody = body.replaceFirst(/(#tableau-diff-pr\\s+\\d+)/) { m -> return "${m[0]} — Part ${partIndex}/${total}" }
                // IMPORTANT: call pullRequest.comment with a String only
                pullRequest.comment(finalBody)
                echo "Posted comment part ${partIndex}/${total}"
                posted = true
              } catch (err) {
                attempt++
                echo "❌ Failed to post comment part ${partIndex}, retrying: ${err}"
                if (attempt >= maxAttempts) {
                  echo "Exceeded retries for comment part ${partIndex}; continuing to next part."
                  break
                }
                sleep time: env.POST_RETRY_DELAY as Integer, unit: 'SECONDS'
              }
            }
            // small pause between comments
            sleep time: 1, unit: 'SECONDS'
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
echo "Commit $SHA - automatic deployment is left to TABLEAU_SYNC_SCRIPT if present."

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

