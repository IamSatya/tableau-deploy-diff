// Jenkinsfile (multibranch) - uses usernamePassword credential for GitHub PAT
// - PR builds run the diff bot (dry-run).
// - main branch build auto-deploys only when the merge commit is from a merged prod->main PR.
// - Uses GenericTrigger for webhook payload mapping.
// Credentials:
//   - github-token : Username with password (password = GitHub PAT)
//   - tableau-cred : Username with password (Tableau username/password)

pipeline {
  agent any

  triggers {
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
          // Only run diffs for PRs (we're only interested in prod -> main PRs and prod-targeted PRs)
          echo "PR build detected (PR #${env.CHANGE_ID}) targeting '${env.CHANGE_TARGET}' -> running diff bot (dry-run)."

          // Bind GitHub PAT (stored as username/password) and Tableau creds
          withCredentials([
            usernamePassword(credentialsId: 'github-token', usernameVariable: 'GITHUB_USER', passwordVariable: 'GITHUB_TOKEN'),
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            sh '''
              . .venv/bin/activate
              export GITHUB_USER="${GITHUB_USER}"
              export GITHUB_TOKEN="${GITHUB_TOKEN}"
              export TABLEAU_USER="${TABLEAU_USER}"
              export TABLEAU_PW="${TABLEAU_PW}"
              export DRY_RUN="${DRY_RUN_DEFAULT}"
              export PR_NUMBER="${CHANGE_ID}"
              export PR_SOURCE_BRANCH="${CHANGE_BRANCH}"
              export PR_TARGET_BRANCH="${CHANGE_TARGET}"
              echo "Running ${TABLEAU_DIFF_PY}..."
              python "${TABLEAU_DIFF_PY}"
            '''
          }
        }
      }
    }

    stage('Deploy to Tableau (main - automatic on prod->main PR merge)') {
      when {
        allOf {
          expression { return env.BRANCH_NAME == 'main' }
          expression { return env.CHANGE_ID == null || env.CHANGE_ID == '' } // not a PR build
        }
      }
      steps {
        script {
          // Determine OWNER/REPO from git remote origin
          def ownerRepo = sh(script: "git config --get remote.origin.url | sed -E 's#.*[:/](.+)/(.+)\\.git\$#\\1/\\2#'", returnStdout: true).trim()
          if (!ownerRepo) {
            error "Unable to determine owner/repo from git remote. Aborting deploy."
          }
          def (owner, repo) = ownerRepo.tokenize('/')

          // Get current commit SHA
          def sha = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
          echo "Main commit ${sha} - querying GitHub for PRs associated with this commit..."

          // Use usernamePassword to supply GitHub PAT (password) and Tableau creds
          withCredentials([
            usernamePassword(credentialsId: 'github-token', usernameVariable: 'GITHUB_USER', passwordVariable: 'GITHUB_TOKEN'),
            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW')
          ]) {
            // Query GitHub API for PRs linked to this commit
            def apiCmd = """curl -s -H "Accept: application/vnd.github.groot-preview+json" \
 -H "Authorization: token ${GITHUB_TOKEN}" \
 "https://api.github.com/repos/${owner}/${repo}/commits/${sha}/pulls" """

            def prListJson = sh(script: apiCmd, returnStdout: true).trim()
            if (!prListJson) {
              error "GitHub API: empty response for commit ${sha}. Aborting deploy."
            }

            // Find a merged PR whose head.ref == "prod"
            def prNumber = sh(script: "echo ${prListJson} | jq -r '.[] | select(.head.ref==\"prod\" and .merged==true) | .number' | head -n1", returnStdout: true).trim()
            def prTitle  = sh(script: "echo ${prListJson} | jq -r '.[] | select(.head.ref==\"prod\" and .merged==true) | .title' | head -n1", returnStdout: true).trim()
            def prUser   = sh(script: "echo ${prListJson} | jq -r '.[] | select(.head.ref==\"prod\" and .merged==true) | .user.login' | head -n1", returnStdout: true).trim()

            if (!prNumber) {
              error "No merged PR from 'prod' found for commit ${sha}. Deployment only runs after a prod->main PR merge."
            }

            echo "Found merged PR #${prNumber} (title: ${prTitle}, author: ${prUser}) associated with commit ${sha}."
            echo "Proceeding automatically to deploy (no manual approval)."

            // Execute the Tableau sync script (real deploy)
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
