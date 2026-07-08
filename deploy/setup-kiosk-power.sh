#!/usr/bin/env bash
# Booth appliance behaviour: auto-login (no password) and NEVER blank / lock / screensaver
# / suspend the screen. Idempotent. Persistent (dconf system db + logind + masked sleep)
# and also applied to the live session so it takes effect without a reboot.
set -euo pipefail
USER_NAME="${1:-pb}"
UID_NUM="$(id -u "$USER_NAME")"

echo "== 1) GDM auto-login (no password at boot) =="
if [ -f /etc/gdm3/custom.conf ]; then
  sudo sed -i \
    -e "s/^#\?\s*AutomaticLoginEnable\s*=.*/AutomaticLoginEnable=true/" \
    -e "s/^#\?\s*AutomaticLogin\s*=.*/AutomaticLogin=$USER_NAME/" /etc/gdm3/custom.conf
  grep -q "AutomaticLoginEnable=true" /etc/gdm3/custom.conf || \
    sudo sed -i "/^\[daemon\]/a AutomaticLoginEnable=true\nAutomaticLogin=$USER_NAME" /etc/gdm3/custom.conf
fi

echo "== 2) never suspend/sleep (systemd + logind) =="
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
sudo mkdir -p /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/photobooth.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
IdleAction=ignore
EOF

echo "== 3) GNOME: no blank/lock/screensaver/auto-suspend (system-wide dconf default) =="
sudo mkdir -p /etc/dconf/db/local.d /etc/dconf/profile
echo -e "user-db:user\nsystem-db:local" | sudo tee /etc/dconf/profile/user >/dev/null
sudo tee /etc/dconf/db/local.d/00-photobooth >/dev/null <<'EOF'
[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/lockdown]
disable-lock-screen=true

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
idle-dim=false
power-button-action='nothing'
EOF
sudo dconf update

echo "== 4) apply to the live session now (no reboot) =="
BUS="unix:path=/run/user/$UID_NUM/bus"
gs(){ sudo -u "$USER_NAME" DBUS_SESSION_BUS_ADDRESS="$BUS" gsettings "$@" 2>/dev/null || true; }
gs set org.gnome.desktop.session idle-delay 0
gs set org.gnome.desktop.screensaver lock-enabled false
gs set org.gnome.desktop.screensaver idle-activation-enabled false
gs set org.gnome.desktop.lockdown disable-lock-screen true
gs set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing
gs set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type nothing
gs set org.gnome.settings-daemon.plugins.power idle-dim false
gs set org.gnome.settings-daemon.plugins.power power-button-action nothing
# X core blanking off (belt and braces; the kiosk launcher also does this)
for d in :0 :1; do
  sudo -u "$USER_NAME" DISPLAY=$d xset s off -dpms 2>/dev/null || true
  sudo -u "$USER_NAME" DISPLAY=$d xset s noblank 2>/dev/null || true
done

echo "done: auto-login=$USER_NAME · no blank/lock/screensaver/suspend"
