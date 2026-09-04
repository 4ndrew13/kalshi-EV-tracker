#!/usr/bin/env bash
# Run ON the VM to deploy a code change. Idempotent.
#   sudo /opt/kalshi-collector/deploy/update.sh
set -euo pipefail
APP=/opt/kalshi-collector

# The ladder is perishable: it exists for four minutes and is then gone forever.
# Restarting through HH:56 costs that hour permanently, so refuse by default.
M=$(date -u +%-M)
if [[ "${FORCE:-0}" != "1" ]] && (( M >= 53 && M <= 58 )); then
  echo "REFUSING: it is HH:${M}m UTC, inside the snapshot window (HH:56)."
  echo "That hour's ladder cannot be recovered. Wait a few minutes, or FORCE=1."
  exit 1
fi

# Skip when deployed by rsync (deploy/push.sh) rather than git.
if [[ "${SKIP_PULL:-0}" != "1" ]] && sudo -u kalshi git -C "$APP" remote get-url origin >/dev/null 2>&1; then
  echo "==> pull"
  # Run git as the repo owner; root on a kalshi-owned tree trips git's
  # dubious-ownership guard, and the deploy key lives in /home/kalshi/.ssh.
  sudo -u kalshi git -C "$APP" pull --ff-only
else
  echo "==> no git remote; using files already in place"
fi

echo "==> deps"
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

# .env, service-account.json and data/ are gitignored and untracked, so a pull
# never touches them. Re-assert ownership in case the pull added files.
chown -R kalshi:kalshi "$APP"
chmod 600 "$APP/.env" "$APP/service-account.json" 2>/dev/null || true

if [[ "${SKIP_SELFTEST:-0}" != "1" ]]; then
  echo "==> selftest (against live APIs)"
  if ! sudo -u kalshi "$APP/.venv/bin/python" -m kalshi_collector.main selftest; then
    echo "SELFTEST FAILED — leaving the running service untouched."
    echo "Fix, or SKIP_SELFTEST=1 if you know why it fails (e.g. prior hour not settled)."
    exit 1
  fi
fi

echo "==> restart"
systemctl restart kalshi-collector
sleep 3
systemctl --no-pager --lines=10 status kalshi-collector || true
echo
echo "Startup backfill runs immediately, so any settlements missed during the"
echo "restart repopulate on their own. Only a missed HH:56 snapshot is permanent."
