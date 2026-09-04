#!/usr/bin/env bash
# Offsite CSV copy. The Sheet holds live buckets only; this preserves the full
# ladder. Run from cron, e.g.  17 * * * *  /opt/kalshi-collector/deploy/backup.sh
set -euo pipefail
APP=/opt/kalshi-collector
cd "$APP"
if [[ -n "${KALSHI_BACKUP_REMOTE:-}" ]]; then
  rsync -az --partial "$APP/data/" "$KALSHI_BACKUP_REMOTE"
fi
if [[ -n "${KALSHI_BACKUP_GIT:-}" ]]; then
  git -C "$APP/data" init -q 2>/dev/null || true
  git -C "$APP/data" add -A
  git -C "$APP/data" -c user.email=collector@localhost -c user.name=collector \
      commit -q -m "data $(date -u +%FT%TZ)" || true
  git -C "$APP/data" push -q "$KALSHI_BACKUP_GIT" HEAD:main || true
fi
