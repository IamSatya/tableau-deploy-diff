pipeline {
    agent any

    environment {
        VENV_DIR = '.venv'
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
                  python3 -m venv ${VENV_DIR}
                  . ${VENV_DIR}/bin/activate
                  pip install --upgrade pip
                  pip install requests python-dotenv jq
                '''
            }
        }

        stage('PR: Run Tableau Diff Bot') {
            when { changeRequest() }
            steps {
                script {
                    echo "PR build detected (PR #${env.CHANGE_ID}) targeting '${env.CHANGE_TARGET}' -> running diff bot."
                    withCredentials([string(credentialsId: 'github-token', variable: 'GITHUB_TOKEN')]) {
                        sh """
                          . ${VENV_DIR}/bin/activate
                          OWNER=IamSatya \
                          REPO=tableau-deploy-diff \
                          PR_NUMBER=${env.CHANGE_ID} \
                          HEAD_BRANCH=${env.CHANGE_BRANCH} \
                          BASE_BRANCH=${env.CHANGE_TARGET} \
                          GITHUB_TOKEN=${GITHUB_TOKEN} \
                          python3 tableau_diff_bot.py > diffbot.log 2>&1 || true
                        """
                    }
                }
            }
        }

        stage('PR: Post Diff Comments') {
            when { changeRequest() }
            steps {
                script {
                    def fileContent = readFile('diffs.txt')
                    def parts = fileContent.split(/(?m)^---COMMENT_PART_\d+---$/)
                    def maxRetries = 3

                    for (int i = 0; i < parts.size(); i++) {
                        def body = parts[i].trim()
                        if (body) {
                            def attempt = 0
                            def posted = false
                            while (attempt < maxRetries && !posted) {
                                try {
                                    echo "📢 Posting PR comment part ${i + 1}/${parts.size()}"
                                    pullRequest.comment(body)   // ✅ Correct usage: String only
                                    posted = true
                                } catch (Exception e) {
                                    attempt++
                                    echo "❌ Failed to post comment part ${i + 1}, attempt ${attempt}: ${e}"
                                    if (attempt < maxRetries) {
                                        sleep(time: 5 * attempt, unit: 'SECONDS') // exponential backoff
                                    } else {
                                        error "Failed to post PR comment after ${maxRetries} attempts"
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        stage('Deploy to Tableau (main - automatic on prod->main PR merge)') {
            when {
                allOf {
                    branch 'main'
                    expression { env.CHANGE_TARGET == 'main' && env.CHANGE_BRANCH == 'prod' }
                }
            }
            steps {
                script {
                    withCredentials([usernamePassword(credentialsId: 'tableau-cred',
                                                      usernameVariable: 'TABLEAU_USER',
                                                      passwordVariable: 'TABLEAU_PW')]) {
                        sh '''
                          . ${VENV_DIR}/bin/activate
                          chmod +x scripts/tableau_sync.sh
                          ./scripts/tableau_sync.sh
                        '''
                    }
                }
            }
        }
    }

    post {
        failure {
            echo "❌ Pipeline failed for branch ${env.BRANCH_NAME} (CHANGE_ID=${env.CHANGE_ID})"
        }
        success {
            echo "✅ Pipeline succeeded for branch ${env.BRANCH_NAME}"
        }
    }
}

