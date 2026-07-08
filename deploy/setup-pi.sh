#!/usr/bin/env bash
# One-time setup on the Orange Pi. Run as root (or with sudo).
#   scp -r ai-photo-booth root@PI:/opt/photobooth && ssh root@PI 'bash /opt/photobooth/deploy/setup-pi.sh'
set -euo pipefail

APP=/opt/photobooth
KIOSK_USER="${KIOSK_USER:-orangepi}"

echo "== installing system packages =="
apt-get update
apt-get install -y python3-venv python3-pip curl rclone chromium-browser \
    python3-libgpiod || apt-get install -y python3-venv python3-pip curl

echo "== python venv =="
cd "$APP"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
# optional extras (don't fail setup if a wheel is unavailable on aarch64)
./venv/bin/pip install opencv-python-headless || echo "(opencv optional - skipped)"
./venv/bin/pip install gpiod || echo "(gpiod via pip optional - python3-libgpiod installed via apt)"

echo "== data dirs =="
mkdir -p "$APP/data/captures"

echo "== power management: keep display + camera awake =="
# Display: stop X blanking / DPMS-off the kiosk screen.
mkdir -p /etc/X11/xorg.conf.d
cp deploy/10-noblank.conf /etc/X11/xorg.conf.d/10-noblank.conf
# Camera: disable USB autosuspend (else the RK3588 suspends the idle Sony device and
# drops the PC-Remote/live-view session). udev rule (per re-enumeration) + kernel cmdline.
cp deploy/50-sony-noautosuspend.rules /etc/udev/rules.d/50-sony-noautosuspend.rules
udevadm control --reload-rules || true
if [ -f /boot/orangepiEnv.txt ] && ! grep -q "usbcore.autosuspend=-1" /boot/orangepiEnv.txt; then
  sed -i 's/^extraargs=.*/& usbcore.autosuspend=-1/' /boot/orangepiEnv.txt
fi

echo "== systemd services =="
cp deploy/photobooth.service /etc/systemd/system/
cp deploy/photobooth-kiosk.service /etc/systemd/system/
sed -i "s/^User=orangepi/User=$KIOSK_USER/" /etc/systemd/system/photobooth-kiosk.service
sed -i "s#/home/orangepi/#/home/$KIOSK_USER/#" /etc/systemd/system/photobooth-kiosk.service
chmod +x deploy/start-kiosk.sh
systemctl daemon-reload
systemctl enable --now photobooth.service
systemctl enable photobooth-kiosk.service   # starts with the graphical session

echo
echo "== done =="
echo "Backend:  http://$(hostname -I | awk '{print $1}'):8000"
echo "Admin:    http://$(hostname -I | awk '{print $1}'):8000/admin"
echo "Kiosk service will launch Chromium on the attached display (reboot or: systemctl start photobooth-kiosk)"
