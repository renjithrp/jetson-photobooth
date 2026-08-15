#!/usr/bin/env bash
# Sync the app to the Jetson and restart the services. Run from your dev machine.
#   ./deploy/deploy.sh [USER@HOST] [--deps] [-n|--dry-run]
#
#   --deps      also refresh the main venv from requirements.txt (slow; only needed
#               when requirements.txt changed)
#   -n          dry run — show what rsync would do, change nothing
#
# NOTE: everything generated ON the Jetson lives under $APP and is excluded below —
# venv/, gesture-venv/ (isolated MediaPipe worker), data/, models/, wheels/, certs/.
# rsync protects --exclude'd paths from --delete, so they survive a deploy.
set -euo pipefail

HOST=pb@192.168.86.30
# a leading non-flag argument overrides the default host
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then HOST="$1"; shift; fi
APP=/opt/photobooth
DEPS=0
DRY=()

for arg in "$@"; do
  case "$arg" in
    --deps)          DEPS=1 ;;
    -n|--dry-run)    DRY=(--dry-run --itemize-changes) ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Python services that run code straight out of the repo — restart after a sync.
# photobooth-camera is the compiled CrSDK daemon; it only changes on a native rebuild.
SERVICES="${SERVICES:-photobooth photobooth-gesture photobooth-captive}"

echo "== rsync -> $HOST:$APP =="
# "${DRY[@]+...}" guards the empty-array expansion so this works under `set -u`
# on macOS's stock bash 3.2 (where a bare "${DRY[@]}" errors when DRY is empty).
rsync -az --delete ${DRY[@]+"${DRY[@]}"} \
  --exclude 'venv/' --exclude 'gesture-venv/' --exclude '.venv/' \
  --exclude 'data/' --exclude 'models/' --exclude 'wheels/' --exclude 'certs/' \
  --exclude '.git/' --exclude '.claude/' --exclude 'ios/' \
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' \
  ./ "$HOST:$APP/"

if [ ${#DRY[@]} -gt 0 ]; then
  echo "(dry run — nothing changed, services not restarted)"
  exit 0
fi

if [ "$DEPS" = 1 ]; then
  echo "== refresh deps =="
  ssh "$HOST" "cd $APP && ./venv/bin/pip install -q -r requirements.txt"
fi

echo "== restart: $SERVICES =="
ssh "$HOST" "sudo systemctl restart $SERVICES && \
  systemctl --no-pager --plain status $SERVICES | grep -E '^(●|\s+Active:|.*\.service)' | head -20"

echo "Done. Admin: http://${HOST#*@}:8000/admin"
