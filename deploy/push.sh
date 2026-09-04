#!/usr/bin/env bash
# Run on YOUR LAPTOP when you have no git remote: rsync the code up and deploy.
#   ./deploy/push.sh opc@<vm-ip>
set -euo pipefail
HOST="${1:?usage: deploy/push.sh user@host}"
APP=/opt/kalshi-collector

# Never push credentials or data. .env and service-account.json live only on the
# VM; data/ is the system of record and must not be overwritten from a laptop.
rsync -az --delete \
  --exclude '.git' --exclude 'data' --exclude '.env' \
  --exclude 'service-account.json' --exclude '__pycache__' \
  --exclude '.venv' --exclude '*.csv' --exclude '*.log' \
  ./ "$HOST:/tmp/kalshi-src/"

ssh "$HOST" "sudo rsync -a --delete \
  --exclude 'data' --exclude '.env' --exclude 'service-account.json' --exclude '.venv' \
  /tmp/kalshi-src/ $APP/ && sudo SKIP_PULL=1 $APP/deploy/update.sh"
