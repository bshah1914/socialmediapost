#!/usr/bin/env bash
# Ship socialmediapost to the server (see docs/switch-over.md before the first live promote).
#
#   deploy/deploy.sh staging    build, send to the staging site and check it
#   deploy/deploy.sh promote    make the image that is on staging the live one (no rebuild)
#   deploy/deploy.sh rollback   go back to the image that was live before the last promote
#   deploy/deploy.sh status     what is running where
#
# Staging never posts anywhere (dry run, no scheduler, no notifications), so it is safe to try things there.
set -euo pipefail

HOST="${DEPLOY_HOST:-brijesh@192.168.50.183}"
IMAGE="socialmediapost"
LIVE_DIR="socialmediapost"
STAGE_DIR="socialmediapost-staging"
LIVE_PORT=7086
STAGE_PORT=7088  # 7087 is the old technews staging, which may still run during the trial
cd "$(dirname "$0")/.."

remote() { ssh "$HOST" "$@"; }

wait_healthy() { # container name, port
  for _ in $(seq 1 30); do
    if remote "curl -fs -m 3 $2/health" >/dev/null 2>&1; then echo "  $1 is healthy"; return 0; fi
    sleep 2
  done
  echo "  $1 did not become healthy" >&2
  return 1
}

case "${1:-}" in
  staging)
    echo "Testing..."
    "${PYTHON:-$( [ -x .venv/Scripts/python.exe ] && echo .venv/Scripts/python.exe || echo python)}" -m pytest tests -q -x 2>&1 | tail -3
    echo "Building..."
    docker build -q -t "$IMAGE:staging" . >/dev/null
    echo "Sending to staging..."
    docker save "$IMAGE:staging" | remote "docker load >/dev/null && cd ~/$STAGE_DIR && docker compose up -d --force-recreate 2>&1 | tail -1"
    wait_healthy socialmediapost-staging "192.168.50.183:$STAGE_PORT"
    echo "Staging: http://192.168.50.183:$STAGE_PORT  (sign in as admin; the password is in ~/$STAGE_DIR/.env on the server)"
    ;;
  promote)
    echo "Promoting the staged image to live..."
    remote "docker image inspect $IMAGE:staging >/dev/null && docker tag $IMAGE:latest $IMAGE:previous && docker tag $IMAGE:staging $IMAGE:latest && cd ~/$LIVE_DIR && docker compose up -d --force-recreate 2>&1 | tail -1"
    if ! wait_healthy socialmediapost "localhost:$LIVE_PORT"; then
      echo "The new version is not healthy: rolling back" >&2
      exec "$0" rollback
    fi
    echo "Live: https://post.cloudtrio.in  (undo with: deploy/deploy.sh rollback)"
    ;;
  rollback)
    echo "Rolling back to the previous image..."
    remote "docker image inspect $IMAGE:previous >/dev/null && docker tag $IMAGE:previous $IMAGE:latest && cd ~/$LIVE_DIR && docker compose up -d --force-recreate 2>&1 | tail -1"
    wait_healthy socialmediapost "localhost:$LIVE_PORT"
    ;;
  status)
    remote "docker ps --filter name=socialmediapost --format '{{.Names}}  {{.Status}}  {{.Image}}'; docker images $IMAGE --format '{{.Tag}}  {{.ID}}  {{.CreatedSince}}'"
    ;;
  *)
    sed -n '2,10p' "$0"
    exit 1
    ;;
esac
