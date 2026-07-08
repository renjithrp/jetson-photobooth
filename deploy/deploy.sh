#!/usr/bin/env bash
# Sync the app to the Pi and restart the backend. Run from your dev machine.
#   ./deploy/deploy.sh [PI_HOST]
set -euo pipefail
PI="${1:-root@192.168.86.105}"
APP=/opt/photobooth

echo "== rsync -> $PI:$APP =="
rsync -az --delete \
  --exclude 'data/' --exclude 'venv/' --exclude '__pycache__/' --exclude '.git/' \
  --exclude 'certs/' --exclude 'models/' \
  ./ "$PI:$APP/"

echo "== refresh deps + restart =="
ssh "$PI" "cd $APP && ([ -d venv ] || python3 -m venv venv) && \
  ./venv/bin/pip install -q -r requirements.txt && \
  systemctl restart photobooth.service && systemctl --no-pager status photobooth.service | head -5"

echo "Done. Admin: http://${PI#*@}:8000/admin"
