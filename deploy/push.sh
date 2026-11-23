#!/usr/bin/env bash
# Deploy (or update) pm-framework from your Mac to a server.
#
#   deploy/push.sh root@SERVER_IP              # first deploy or any later update
#   deploy/push.sh root@SERVER_IP pm.mydomain.com   # use your own domain
#
# First deploy uploads the code, your .env, keys and the local database.
# Later runs upload code only: the server's .env, keys and database are the
# live copies from then on and are never overwritten.
set -euo pipefail
TARGET=${1:?usage: deploy/push.sh root@SERVER_IP [domain]}
DOMAIN=${2:-}
cd "$(dirname "$0")/.."

EXCLUDES=(--exclude .git --exclude .venv --exclude __pycache__ --exclude .pytest_cache
          --exclude .DS_Store --exclude 'db/markets.backup-*' --exclude dashboard/feed.xml
          --exclude verified_pairs.csv)
if ssh "$TARGET" test -f /opt/pm-framework/.env; then
  echo "Updating existing deploy (server .env, keys and database left untouched)"
  EXCLUDES+=(--exclude .env --exclude 'db/*.duckdb' --exclude 'db/*.duckdb.wal'
             --exclude '*.pem' --exclude '*.key' --exclude 'kalshi_private*.txt')
else
  echo "First deploy: uploading code, .env, keys and database"
fi

ssh "$TARGET" mkdir -p /opt/pm-framework
rsync -az "${EXCLUDES[@]}" ./ "$TARGET:/opt/pm-framework/"
ssh "$TARGET" "DOMAIN='$DOMAIN' bash /opt/pm-framework/deploy/bootstrap.sh"
