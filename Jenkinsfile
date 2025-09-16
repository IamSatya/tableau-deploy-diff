// Jenkinsfile (multibranch) - fixed: inject OWNER/REPO/HEAD/BASE/PR envs for PR runs
pipeline {
  agent any

  triggers {
    // keep GenericTrigger if you still want webhook-based runs; for multibranch PR jobs GitHub Branch Source is used.
    GenericTrigger(
      genericVariables: [
        [key: 'OWNER',       value: '$.repository.owner.login'],
        [key: 'REPO',        value: '$.repository.name'],
        [key: 'PR_NUMBER',   value: '$.pull_request.number'],
        [key: 'HEAD_BRANCH', value: '$.pull_request.head.ref'],
        [key: 'BASE_BRANCH', value: '$.pull_request.base.ref'],
        [key: 'ACTION',      value: '$.action'],
        [key: 'MERGED',      value: '$.pull_request.merged']
      ],
      causeString: 'Triggered by GitHub Pull Request Webhook',
      token: 'my-secret-webhook-token',
      printContributedVariables: true,
      printPostContent: false,
      silentResponse: false
    )
  }

  options {
    disableConcurrentBuilds()
    timestamps()
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
          # create venv and install deps
          python3 -m venv .venv || python -m venv .venv
          . .venv/bin/activate
          pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt || true; else pip install requests python-dotenv jq || true; fi
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

          // Build OWNER/REPO from git remote if not provided via webhook (multibranch builds typically don't have them)
          def ownerRepo = sh(script: "git config --get remote.origin.url | sed -E 's#.*[:/](.+)/(.+)\\.git\$#\\1/\\2#' || true", returnStdout: true).trim()
          if (!ownerRepo) {
            // fallback: try parsing GIT_URL env or repository info
            ownerRepo = "${env.OWNER ?: ''}/${env.REPO ?: ''}"
            ownerRepo = ownerRepo.trim().replaceAll('^/','').replaceAll('/$','')
          }
          if (!ownerRepo || ownerRepo == '/') {
            echo "WARNING: Could not determine owner/repo from git remote or webhook. OWNER/REPO will be empty unless webhook provided them."
          } else {
            echo "Determined owner/repo: ${ownerRepo}"
          }
          def owner = ownerRepo.tokenize('/').size() >= 2 ? ownerRepo.tokenize('/')[0] : ''
          def repo  = ownerRepo.tokenize('/').size() >= 2 ? ownerRepo.tokenize('/')[1] : ''

          // Map PR-specific envs from multibranch vars (CHANGE_*)
          def prNumber = env.CHANGE_ID ?: env.PR_NUMBER ?: ''
          def headBranch = env.CHANGE_BRANCH ?: env.HEAD_BRANCH ?: ''
          def baseBranch = env.CHANGE_TARGET ?: env.BASE_BRANCH ?: ''

          if (!prNumber) {
            error "PR number not available (CHANGE_ID/PR_NUMBER). Aborting PR diff stage."
          }

          echo "PR envs: OWNER='${owner}', REPO='${repo}', PR_NUMBER='${prNumber}', HEAD_BRANCH='${headBranch}', BASE_BRANCH='${baseBranch}'"

          // Bind creds and run diff bot
          withCredentials([
            usernamePassword(credentialsId: 'github-token', usernameVariable: 'GITHUB_USER', passwordVariable: 'GITHUB_TOKEN'),
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            // Export variables and call the Python diff bot
            sh """
              set -euo pipefail
              . .venv/bin/activate
              export GITHUB_USER='${GITHUB_USER}'
              export GITHUB_TOKEN='${GITHUB_TOKEN}'
              export TABLEAU_USER='${TABLEAU_USER}'
              export TABLEAU_PW='${TABLEAU_PW}'
              export DRY_RUN='${DRY_RUN_DEFAULT}'
              export PR_NUMBER='${prNumber}'
              export PR_SOURCE_BRANCH='${headBranch}'
              export PR_TARGET_BRANCH='${baseBranch}'
              export OWNER='${owner}'
              export REPO='${repo}'
              echo "Invoking ${TABLEAU_DIFF_PY} with OWNER=${owner}, REPO=${repo}, PR_NUMBER=${prNumber}, HEAD=${headBranch}, BASE=${baseBranch}"
              python "${TABLEAU_DIFF_PY}"
            """
          } // withCredentials
        } // script
      } // steps
    } // stage PR

    stage('Deploy to Tableau (main - automatic on prod->main PR merge)') {
      when {
        allOf {
          expression { return env.BRANCH_NAME == 'main' }
          expression { return env.CHANGE_ID == null || env.CHANGE_ID == '' } // not a PR build
        }
      }
      steps {
        script {
          echo "Main branch build detected. Checking if this commit is from a merged prod->main PR..."

          // Determine owner/repo
          def ownerRepo = sh(script: "git config --get remote.origin.url | sed -E 's#.*[:/](.+)/(.+)\\.git\$#\\1/\\2#' || true", returnStdout: true).trim()
          if (!ownerRepo) {
            error "Unable to determine owner/repo from git remote. Aborting deploy."
          }
          def (owner, repo) = ownerRepo.tokenize('/')

          def sha = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
          echo "Main commit ${sha} - querying GitHub for PRs associated with this commit..."

          withCredentials([
            usernamePassword(credentialsId: 'github-token', usernameVariable: 'GITHUB_USER', passwordVariable: 'GITHUB_TOKEN'),
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            def apiCmd = """curl -s -H "Accept: application/vnd.github.groot-preview+json" \
 -H "Authorization: token ${GITHUB_TOKEN}" \
 "https://api.github.com/repos/${owner}/${repo}/commits/${sha}/pulls" """

            def prListJson = sh(script: apiCmd, returnStdout: true).trim()
            if (!prListJson) {
              error "GitHub API: empty response for commit ${sha}. Aborting deploy."
            }

            def prNumber = sh(script: "echo ${prListJson} | jq -r '.[] | select(.head.ref==\"prod\" and .merged==true) | .number' | head -n1", returnStdout: true).trim()
            def prTitle  = sh(script: "echo ${prListJson} | jq -r '.[] | select(.head.ref==\"prod\" and .merged==true) | .title' | head -n1", returnStdout: true).trim()
            def prUser   = sh(script: "echo ${prListJson} | jq -r '.[] | select(.head.ref==\"prod\" and .merged==true) | .user.login' | head -n1", returnStdout: true).trim()

            if (!prNumber) {
              error "No merged PR from 'prod' found for commit ${sha}. Deployment only runs after a prod->main PR merge."
            }

            echo "Found merged PR #${prNumber} (title: ${prTitle}, author: ${prUser}) associated with commit ${sha}."
            echo "Proceeding automatically to deploy (no manual approval)."

            sh """
              chmod +x "${TABLEAU_SYNC_SCRIPT}" || true
              export TABLEAU_USER="${TABLEAU_USER}"
              export TABLEAU_PW="${TABLEAU_PW}"
              export DRY_RUN="false"
              export WORKSPACE="${WORKSPACE}"
              echo "Invoking sync script: ${TABLEAU_SYNC_SCRIPT}"
              "${TABLEAU_SYNC_SCRIPT}"
            """
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
