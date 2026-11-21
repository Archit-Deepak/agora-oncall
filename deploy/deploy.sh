#!/usr/bin/env bash
# Push the code to the VM and restart. No git involved (see HANDOFF: no commits).
#   bash deploy/deploy.sh            # code only
#   bash deploy/deploy.sh --env      # also copy .env (secrets) to the VM, chmod 600
#   bash deploy/deploy.sh --setup    # first time: venv, deps, systemd unit, Caddyfile
set -euo pipefail
cd "$(dirname "$0")/.."
VM=archit@52.231.69.243
KEY=~/.ssh/agora_oncall_vm
SSH="ssh -i $KEY -o IdentitiesOnly=yes"
APP=/home/archit/agora-oncall

rsync -az --delete -e "$SSH" \
  --exclude '.git/' --exclude '.venv/' --exclude 'state/' --exclude '__pycache__/' \
  --exclude '.pytest_cache/' --exclude '.env' --exclude '.env.*' --exclude '*.log' --exclude '.DS_Store' --exclude '.playwright-mcp/' --exclude '.snapshots/' \
  ./ "$VM:$APP/"

if [[ " $* " == *" --env "* ]]; then
  $SSH "$VM" "umask 077; cat > $APP/.env" < .env
  $SSH "$VM" "chmod 600 $APP/.env"
  echo "copied .env"
fi

if [[ " $* " == *" --setup "* ]]; then
  $SSH "$VM" "cd $APP && bash deploy/setup_vm.sh"
else
  $SSH "$VM" "cd $APP && .venv/bin/pip install -q -r requirements.txt && sudo systemctl restart agora-oncall && sleep 2 && systemctl is-active agora-oncall"
fi
