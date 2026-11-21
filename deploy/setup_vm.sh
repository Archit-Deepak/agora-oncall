#!/usr/bin/env bash
# One-time (idempotent) server setup. Run ON the VM from the app dir: bash deploy/setup_vm.sh
# Installs the venv + deps, the systemd unit and the Caddyfile, then (re)starts both.
set -euo pipefail
APP=/home/archit/agora-oncall
cd "$APP"

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

sudo install -m 644 deploy/agora-oncall.service /etc/systemd/system/agora-oncall.service
sudo install -m 644 deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable -q agora-oncall
sudo systemctl restart agora-oncall
sudo systemctl reload caddy || sudo systemctl restart caddy
sleep 2
systemctl is-active agora-oncall caddy
