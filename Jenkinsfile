pipeline {
    agent any

    environment {
        // GitHub auth token stored in Jenkins Credentials
        GITHUB_TOKEN = credentials('github-token')

        // Jenkins job or webhook should populate these
        OWNER        = "${env.OWNER}"
        REPO         = "${env.REPO}"
        PR_NUMBER    = "${env.PR_NUMBER}"
        HEAD_BRANCH  = "${env.HEAD_BRANCH}"
        BASE_BRANCH  = "${env.BASE_BRANCH}"
    }

    stages {
        stage('Setup') {
            steps {
                sh 'python --version'
                sh 'pip install requests python-dotenv'
            }
        }

        stage('Run Tableau Diff Bot') {
            steps {
                sh 'python tableau_diff_bot.py'
            }
        }
    }

    post {
        success {
            echo "Tableau diff bot completed successfully"
        }
        failure {
            echo "Tableau diff bot failed"
        }
    }
}
