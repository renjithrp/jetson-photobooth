#!/usr/bin/env bash
# Launch the kiosk browser fullscreen pointing at the local booth UI.
# Runs inside the pb GNOME/X11 session (via ~/.config/autostart on the Jetson).
set -e
PORT="${BOOTH_PORT:-8000}"
URL="http://localhost:$PORT/"
CERTFLAG=""
if [ -f /opt/photobooth/certs/cert.pem ]; then
  URL="https://localhost:$PORT/"
  CERTFLAG="--ignore-certificate-errors --test-type"
fi

# Resolve a working DISPLAY (Jetson GNOME is usually :1, not :0).
if [ -z "${DISPLAY:-}" ] || ! xset -q >/dev/null 2>&1; then
  for d in :0 :1 :2; do
    if DISPLAY=$d xset -q >/dev/null 2>&1; then export DISPLAY=$d; break; fi
  done
fi

# Wait for the backend to answer before opening the browser.
until curl -skf "$URL" >/dev/null || curl -sf "http://localhost:$PORT/" >/dev/null; do sleep 1; done

# pick whichever chromium is installed (snap symlinks to /snap/bin/chromium)
BIN="$(command -v chromium || command -v chromium-browser || command -v google-chrome || echo /snap/bin/chromium)"
if [ ! -x "$BIN" ] && ! command -v "$BIN" >/dev/null 2>&1; then
  echo "No chromium found. Install with: sudo snap install chromium" >&2
  exit 1
fi

# Disable screen blanking / lock (GNOME + X core) so the kiosk never goes dark.
xset s off -dpms 2>/dev/null || true
xset s noblank 2>/dev/null || true
gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null || true
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null || true

exec "$BIN" $CERTFLAG \
  --kiosk "$URL" \
  --incognito --disk-cache-size=1 \
  --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-translate \
  --autoplay-policy=no-user-gesture-required \
  --check-for-update-interval=31536000 \
  --overscroll-history-navigation=0 \
  --password-store=basic
