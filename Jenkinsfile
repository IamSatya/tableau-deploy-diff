// Jenkinsfile (multibranch) - automatic deploy after prod->main PR merged (no manual approval)

pipeline {
  agent any

  options {
    disableConcurrentBuilds()
    timestamps()
  }

  environment {
    TABLEAU_SYNC_SCRIPT = 'scripts/tableau_sync.sh'
    TABLEAU_DIFF_PY     = 'tableau_diff_bot.py'
    DRY_RUN_DEFAULT = 'true'
  }

  stages {
    stage('Checkout') {
      steps {
        echo "Checking out source for branch: ${env.BRANCH_NAME} (CHANGE_ID=${env.CHANGE_ID}, CHANGE_TARGET=${env.CHANGE_TARGET})"
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
      when { expression { return env.CHANGE_ID != null && env.CHANGE_ID != '' } }
      steps {
        echo "PR build: running diff bot (dry-run)."
        withCredentials([ string(credentialsId: 'github-token', variable: 'GITHUB_TOKEN'),
                          usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW') ]) {
          sh '''
            . .venv/bin/activate
            export GITHUB_TOKEN="${GITHUB_TOKEN}"
            export TABLEAU_USER="${TABLEAU_USER}"
            export TABLEAU_PW="${TABLEAU_PW}"
            export DRY_RUN="${DRY_RUN_DEFAULT}"
            export PR_NUMBER="${CHANGE_ID}"
            export PR_SOURCE_BRANCH="${CHANGE_BRANCH}"
            export PR_TARGET_BRANCH="${CHANGE_TARGET}"
            python "${TABLEAU_DIFF_PY}"
          '''
        }
      }
    }

    stage('Deploy to Tableau (main, automatic on prod->main PR merge)') {
      when {
        allOf {
          expression { return env.BRANCH_NAME == 'main' }
          expression { return env.CHANGE_ID == null || env.CHANGE_ID == '' }
        }
      }

      steps {
        script {
          // Determine OWNER/REPO from git remote origin
          def ownerRepo = sh(script: "git config --get remote.origin.url | sed -E 's#.*[:/](.+)/(.+)\\.git\$#\\1/\\2#'", returnStdout: true).trim()
          if (!ownerRepo) {
            error "Could not determine repository owner/name from git remote origin URL. Aborting deploy stage."
          }
          def (owner, repo) = ownerRepo.tokenize('/')

          // Get current commit sha
          def sha = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
          echo "Detected commit ${sha} on main. Querying GitHub for PRs associated with this commit..."

          // Query GitHub for PRs associated with this commit using the GitHub token
          withCredentials([ string(credentialsId: 'github-token', variable: 'GITHUB_TOKEN'),
                            usernamePassword(credentialsId: 'tableau-cred', usernameVariable: 'TABLEAU_USER', passwordVariable: 'TABLEAU_PW') ]) {

            def apiCmd = """curl -s -H "Accept: application/vnd.github.groot-preview+json" \
 -H "Authorization: token ${GITHUB_TOKEN}" \
 "https://api.github.com/repos/${owner}/${repo}/commits/${sha}/pulls" """

            def prListJson = sh(script: apiCmd, returnStdout: true).trim()
            if (!prListJson) {
              error "GitHub API returned empty response when searching PRs for commit ${sha}. Aborting deploy stage."
            }

            // Find a merged PR whose head.ref == "prod"
            def prNumber = sh(script: "echo ${prListJson} | jq -r '.[] | select(.head.ref==\"prod\" and .merged==true) | .number' | head -n1", returnStdout: true).trim()
            def prTitle  = sh(script: "echo ${prListJson} | jq -r '.[] | select(.head.ref==\"prod\" and .merged==true) | .title' | head -n1", returnStdout: true).trim()
            def prUser   = sh(script: "echo ${prListJson} | jq -r '.[] | select(.head.ref==\"prod\" and .merged==true) | .user.login' | head -n1", returnStdout: true).trim()

            if (!prNumber) {
              // No merged PR from prod found for this commit -> abort safe
              error "No merged PR with head branch 'prod' found for commit ${sha}. Aborting deploy; deployment allowed only after a prod->main PR merge."
            }

            echo "Found merged PR #${prNumber} (title: ${prTitle}, author: ${prUser}) associated with commit ${sha}."
            echo "Proceeding automatically to deploy (no manual approval)."

            // Run the sync script (with DRY_RUN=false)
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
    } // stage
  } // stages

  post {
    success { echo "✅ Pipeline completed successfully for ${env.BRANCH_NAME}" }
    failure { echo "❌ Pipeline failed for ${env.BRANCH_NAME}" }
  }
}
