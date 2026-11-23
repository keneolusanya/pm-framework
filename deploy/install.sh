#!/usr/bin/env bash
# Install / refresh the pm-framework systemd units on the server.
#   sudo deploy/install.sh        (deploy/bootstrap.sh runs this for you)
# Expects the repo at /opt/pm-framework with .venv and .env in place, owned by
# the `pm` user the units run as.
set -euo pipefail
cd "$(dirname "$0")"
install -m 0644 pm-*.service pm-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pm-ingest.timer pm-scan.timer pm-summary.timer pm-prune.timer
systemctl enable pm-webhook.service pm-dashboard.service
systemctl restart pm-webhook.service pm-dashboard.service  # pick up new code on updates
systemctl list-timers 'pm-*' --no-pager
