pipeline {
    agent any

    environment {
        GITHUB_TOKEN = credentials('github-token')
    }

    triggers {
        // This assumes the job is triggered by GitHub webhook
        GenericTrigger(
            genericVariables: [
                [key: 'OWNER',       value: '$.repository.owner.login'],
                [key: 'REPO',        value: '$.repository.name'],
                [key: 'PR_NUMBER',   value: '$.pull_request.number'],
                [key: 'HEAD_BRANCH', value: '$.pull_request.head.ref'],
                [key: 'BASE_BRANCH', value: '$.pull_request.base.ref']
            ],
            causeString: 'Triggered by GitHub Pull Request Webhook',
            token: 'my-secret-webhook-token',   // set the same secret in GitHub webhook config
            printContributedVariables: true,
            printPostContent: true,
            silentResponse: false
        )
    }

    stages {
        stage('Debug Env') {
            steps {
                sh '''
                    echo "OWNER=$OWNER"
                    echo "REPO=$REPO"
                    echo "PR_NUMBER=$PR_NUMBER"
                    echo "HEAD_BRANCH=$HEAD_BRANCH"
                    echo "BASE_BRANCH=$BASE_BRANCH"
                '''
            }
        }

        stage('Setup Python Env') {
            steps {
                sh '''
                    python3 -m venv venv
                    . venv/bin/activate
                    pip install --upgrade pip
                    pip install requests python-dotenv
                '''
            }
        }

        stage('Run Tableau Diff Bot') {
            steps {
                sh '''
                    . venv/bin/activate
                    python tableau_diff_bot.py
                '''
            }
        }
    }

    post {
        success {
            echo "✅ Tableau diff bot completed successfully"
        }
        failure {
            echo "❌ Tableau diff bot failed"
        }
    }
}
