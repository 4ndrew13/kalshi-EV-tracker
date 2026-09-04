#!/usr/bin/env bash
# Idempotent installer for the OCI VM. Safe to re-run.
set -euo pipefail

APP=/opt/kalshi-collector
REPO="${1:-}"

echo "==> packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git rsync

echo "==> service user"
id -u kalshi &>/dev/null || sudo useradd --system --create-home --shell /usr/sbin/nologin kalshi

echo "==> app dir"
sudo mkdir -p "$APP"
if [[ -n "$REPO" && ! -d "$APP/.git" ]]; then
  sudo git clone "$REPO" "$APP"
elif [[ -d "$APP/.git" ]]; then
  sudo git -C "$APP" pull --ff-only || true
fi
sudo mkdir -p "$APP/data"

echo "==> venv"
sudo python3 -m venv "$APP/.venv"
sudo "$APP/.venv/bin/pip" install -q --upgrade pip
sudo "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "==> permissions"
sudo chown -R kalshi:kalshi "$APP"
[[ -f "$APP/service-account.json" ]] && sudo chmod 600 "$APP/service-account.json" || true
[[ -f "$APP/.env" ]] && sudo chmod 600 "$APP/.env" || true

echo "==> clock must be UTC (one unambiguous clock in logs)"
sudo timedatectl set-timezone UTC

echo "==> systemd"
sudo cp "$APP/deploy/kalshi-collector.service" /etc/systemd/system/
sudo systemctl daemon-reload

cat <<'MSG'

Installed. Before starting the service, run the self-test:

  sudo -u kalshi /opt/kalshi-collector/.venv/bin/python \
       -m kalshi_collector.main selftest

It verifies Kalshi pagination, ladder resolution, expiration_value, the
three-venue composite, historical trade reconstruction, and the measured basis
against BRTI. Do NOT start the service until it passes -- the exchange endpoints
were not verifiable from the build machine.

Then:
  sudo systemctl enable --now kalshi-collector
  journalctl -u kalshi-collector -f

MSG
