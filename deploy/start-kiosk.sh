#!/usr/bin/env bash
# Launch the kiosk browser fullscreen pointing at the local booth UI.
set -e
PORT="${BOOTH_PORT:-8000}"
URL="http://localhost:$PORT/"
CERTFLAG=""
# match the backend scheme; accept the self-signed cert when HTTPS is enabled
if [ -f /opt/photobooth/certs/cert.pem ]; then
  URL="https://localhost:$PORT/"
  CERTFLAG="--ignore-certificate-errors --test-type"
fi

# pick whichever chromium is installed
BIN="$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)"
if [ -z "$BIN" ]; then
  echo "No chromium found. Install with: sudo apt install -y chromium-browser" >&2
  exit 1
fi

# disable screen blanking / power management
xset s off -dpms 2>/dev/null || true
xset s noblank 2>/dev/null || true
# xfce4-power-manager re-enables DPMS after login (overriding both xset and the xorg
# 10-noblank.conf), which puts the monitor into Standby and blanks the kiosk. Turn it
# off at the source. Persists via xfconf; harmless where xfce/xfconf isn't present.
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
xfconf-query -c xfce4-power-manager -p /xfce4-power-manager/dpms-enabled -s false 2>/dev/null || true

# Chromium refuses to run as root without --no-sandbox (the Orange Pi desktop is root)
SANDBOX=""
[ "$(id -u)" = "0" ] && SANDBOX="--no-sandbox"

exec "$BIN" $SANDBOX $CERTFLAG \
  --kiosk "$URL" \
  --incognito --disk-cache-size=1 \
  --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-translate \
  --autoplay-policy=no-user-gesture-required \
  --check-for-update-interval=31536000 \
  --overscroll-history-navigation=0 \
  --disable-accelerated-mjpeg-decode \
  --disable-accelerated-video-decode \
  --disable-features=VaapiVideoDecoder,VaapiJpegDecoder \
  --disable-gpu-compositing
