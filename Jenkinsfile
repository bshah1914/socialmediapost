// CloudTrio Newsroom — build & deploy pipeline.
//
// Assumes the Jenkins agent runs on (or shares a Docker daemon with) the host
// that runs the app, and that ${DEPLOY_DIR} holds the real docker-compose.yml
// and .env (with secrets). The pipeline builds the image, backs up the current
// one, promotes, restarts via docker compose, health-checks, and rolls back on
// failure.
//
// If Jenkins is on a DIFFERENT host, change the "Promote & deploy" and
// "Health check" stages to run their shell over SSH to the deploy host instead
// (see the commented sshDeploy() helper at the bottom).

pipeline {
  agent any

  parameters {
    string(name: 'DEPLOY_DIR', defaultValue: '/home/brijesh/cloudtrio-newsroom',
           description: 'Dir on the deploy host holding docker-compose.yml + .env')
    string(name: 'IMAGE',      defaultValue: 'cloudtrio/newsroom', description: 'Docker image name')
    string(name: 'LIVE_PORT',  defaultValue: '7086', description: 'Host port the live app listens on')
  }

  options {
    timestamps()
    disableConcurrentBuilds()
    timeout(time: 25, unit: 'MINUTES')
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
        sh 'git log --oneline -1'
      }
    }

    stage('Build image') {
      steps {
        sh "docker build -t ${params.IMAGE}:candidate ."
      }
    }

    stage('Backup current') {
      steps {
        sh """
          if docker image inspect ${params.IMAGE}:latest >/dev/null 2>&1; then
            docker tag ${params.IMAGE}:latest ${params.IMAGE}:previous
            echo 'Backed up latest -> previous'
          else
            echo 'No existing :latest to back up (first deploy)'
          fi
        """
      }
    }

    stage('Promote & deploy') {
      steps {
        sh """
          docker tag ${params.IMAGE}:candidate ${params.IMAGE}:latest
          cd ${params.DEPLOY_DIR}
          docker compose up -d --force-recreate
        """
      }
    }

    stage('Health check') {
      steps {
        sh """
          for i in \$(seq 1 20); do
            if curl -fs http://localhost:${params.LIVE_PORT}/health >/dev/null 2>&1; then
              echo 'Live is healthy'
              exit 0
            fi
            echo "waiting for health... (\$i)"
            sleep 3
          done
          echo 'Health check never passed'
          exit 1
        """
      }
    }
  }

  post {
    success {
      echo "Deployed ${params.IMAGE}:latest successfully."
    }
    failure {
      echo 'Deploy failed — rolling back to :previous'
      sh """
        if docker image inspect ${params.IMAGE}:previous >/dev/null 2>&1; then
          docker tag ${params.IMAGE}:previous ${params.IMAGE}:latest
          cd ${params.DEPLOY_DIR}
          docker compose up -d --force-recreate || true
          echo 'Rolled back to previous image'
        else
          echo 'No :previous image to roll back to'
        fi
      """
    }
  }
}

// ---------------------------------------------------------------------------
// If Jenkins runs on a different host than the app, replace the shell in the
// "Promote & deploy" / "Health check" stages with SSH, e.g.:
//
//   withCredentials([sshUserPrivateKey(credentialsId: 'newsroom-deploy-key',
//                                      keyFileVariable: 'KEY', usernameVariable: 'USER')]) {
//     sh '''
//       docker save ${IMAGE}:candidate | ssh -i $KEY -o StrictHostKeyChecking=no \
//         $USER@192.168.50.183 "docker load && \
//           docker tag ${IMAGE}:latest ${IMAGE}:previous; \
//           docker tag ${IMAGE}:candidate ${IMAGE}:latest && \
//           cd ${DEPLOY_DIR} && docker compose up -d --force-recreate"
//     '''
//   }
