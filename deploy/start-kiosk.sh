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

# Render at 1080p, not the panel's native 4K. The Chromium snap has no GPU access on
# this Jetson (strict confinement can't reach the Tegra EGL driver), so it composites
# the live view in SOFTWARE: 4K@30 burns ~2.5 CPU cores, 1080p@60 ~1.7 and is smoother.
# The Sony live-view source is only ~1024px, so no real detail is lost. Override with
# KIOSK_RES / KIOSK_RATE (e.g. KIOSK_RES=4096x2160 to restore native 4K).
KRES="${KIOSK_RES:-1920x1080}"; KRATE="${KIOSK_RATE:-60}"
KOUT="$(xrandr 2>/dev/null | awk '/ connected/{print $1; exit}')"
if [ -n "$KOUT" ] && [ "$KRES" != "native" ]; then
  xrandr --output "$KOUT" --mode "$KRES" --rate "$KRATE" 2>/dev/null || true
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

# Launch in the background (not exec) so we can force the window truly
# fullscreen afterwards. Chromium's --kiosk is unreliable under GNOME/mutter on
# this 4K panel: the window can open decorated and half-tiled instead of
# filling the screen. We ask the window manager to fullscreen it directly.
"$BIN" $CERTFLAG \
  --kiosk "$URL" \
  --start-fullscreen \
  --incognito --disk-cache-size=1 \
  --noerrdialogs --disable-infobars --no-first-run \
  --disable-session-crashed-bubble --disable-translate \
  --autoplay-policy=no-user-gesture-required \
  --check-for-update-interval=31536000 \
  --overscroll-history-navigation=0 \
  --password-store=basic &
KPID=$!

# Belt-and-braces: re-assert real fullscreen via the WM until the window shows
# up and accepts it (title becomes "Photo Booth" once the page loads).
if command -v wmctrl >/dev/null 2>&1; then
  ( for _ in $(seq 1 30); do
      if wmctrl -l 2>/dev/null | grep -q "Photo Booth"; then
        wmctrl -r "Photo Booth" -b add,fullscreen 2>/dev/null || true
      fi
      sleep 1
    done ) &
fi

wait "$KPID"
