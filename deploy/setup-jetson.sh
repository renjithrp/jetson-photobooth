#!/usr/bin/env bash
# One-time setup for PhotoBooth Pro on the Jetson Orin Nano (JetPack 7 / Ubuntu 24.04).
# Idempotent — safe to re-run. Run as the booth user with passwordless sudo:
#   rsync -az ./ pb@JETSON:/opt/photobooth/ && ssh pb@JETSON 'bash /opt/photobooth/deploy/setup-jetson.sh'
set -euo pipefail

APP=/opt/photobooth
CRSDK=/opt/CrSDK
KIOSK_USER="${KIOSK_USER:-pb}"
KIOSK_UID="$(id -u "$KIOSK_USER")"

say() { echo -e "\n== $* =="; }

wait_apt() {   # JetPack/unattended-upgrades may hold the dpkg lock; wait it out
  while sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
    echo "  (waiting for apt lock...)"; sleep 5
  done
}

say "system packages"
wait_apt
sudo apt-get update -y
wait_apt
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3.12-venv python3-pip curl git build-essential rclone \
  network-manager v4l-utils cups printer-driver-gutenprint || true
# Chromium ships as a snap on Ubuntu 24.04
if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  say "chromium (snap)"
  sudo snap install chromium || echo "  (chromium install failed — kiosk optional; retry: sudo snap install chromium)"
fi

say "python venv + deps"
cd "$APP"
if [ ! -x "$APP/venv/bin/python" ]; then
  python3 -m venv --without-pip venv
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  ./venv/bin/python /tmp/get-pip.py -q
fi
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

say "data dirs"
mkdir -p "$APP/data/captures" "$APP/data/incoming" "$APP/models"

say "build Sony CrSDK helpers"
if [ -d "$CRSDK/app" ]; then
  CRSDK="$CRSDK" bash "$APP/native/build.sh" || echo "  (CrSDK build failed — check $CRSDK)"
else
  echo "  CrSDK not found at $CRSDK — copy it there, then re-run (see SPEC §5.1)."
fi

say "USB power management (keep the Sony camera link alive)"
sudo cp "$APP/deploy/50-sony-noautosuspend.rules" /etc/udev/rules.d/ 2>/dev/null || true
sudo udevadm control --reload-rules 2>/dev/null || true

say "systemd services"
sudo cp "$APP/deploy/photobooth.service" /etc/systemd/system/
sudo cp "$APP/deploy/photobooth-camera.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now photobooth.service
sudo systemctl enable --now photobooth-camera.service || true   # waits for camera

say "kiosk autostart ($KIOSK_USER GNOME session)"
install -d -o "$KIOSK_USER" -g "$KIOSK_USER" "/home/$KIOSK_USER/.config/autostart"
sudo cp "$APP/deploy/photobooth-kiosk.desktop" "/home/$KIOSK_USER/.config/autostart/"
sudo chown "$KIOSK_USER:$KIOSK_USER" "/home/$KIOSK_USER/.config/autostart/photobooth-kiosk.desktop"

say "auto-login (no password) + never blank/lock/screensaver/suspend"
bash "$APP/deploy/setup-kiosk-power.sh" "$KIOSK_USER"

IP="$(hostname -I | awk '{print $1}')"
say "done"
echo "Backend : http://$IP:8000"
echo "Admin   : http://$IP:8000/admin   (PIN 1234 — change it in Admin → General)"
echo "Kiosk   : starts on the attached monitor at next login (or: bash $APP/deploy/start-kiosk.sh)"
echo "Camera  : plug in the Sony (PC Remote, Save Dest = PC), then: systemctl status photobooth-camera"
