#!/usr/bin/env bash
# Enable (or disable) the guest captive portal so ONE Wi-Fi QR both joins the booth
# hotspot AND auto-opens the find-your-photos page on the guest's phone.
#
# Two pieces:
#   1) DNS hijack — copies captive-dnsmasq.conf into NetworkManager's shared-dnsmasq
#      dir so every connectivity probe on the hotspot resolves to the booth.
#   2) photobooth-captive.service — a plain-HTTP server on :80 that fires the captive
#      popup and serves the guest page/downloads (proxying the HTTPS backend).
#
#   sudo ./setup-captive.sh install     # install + enable + start (default)
#   sudo ./setup-captive.sh remove      # tear down, restore guest internet
#   ./setup-captive.sh status
#
# Prereq: the hotspot is already up (deploy/setup-hotspot.sh up). Run this ON the Pi.
set -euo pipefail

APP="${APP:-/opt/photobooth}"
ACTION="${1:-install}"
AP_CON=photobooth-ap
DNSMASQ_DIR=/etc/NetworkManager/dnsmasq-shared.d
DNSMASQ_DST="$DNSMASQ_DIR/photobooth-captive.conf"

reactivate_ap() {
  # NetworkManager only (re)reads dnsmasq-shared.d when the shared connection comes up.
  if nmcli -t -f NAME connection show --active 2>/dev/null | grep -qx "$AP_CON"; then
    echo "== reactivating $AP_CON so dnsmasq reloads =="
    nmcli connection down "$AP_CON" >/dev/null 2>&1 || true
    nmcli connection up   "$AP_CON" >/dev/null 2>&1 || \
      echo "  (couldn't bring $AP_CON up — bring the hotspot up first: setup-hotspot.sh up)"
  else
    echo "  ($AP_CON not active — DNS hijack applies next time the hotspot comes up)"
  fi
}

case "$ACTION" in
  install)
    echo "== installing captive DNS hijack =="
    mkdir -p "$DNSMASQ_DIR"
    cp "$APP/deploy/captive-dnsmasq.conf" "$DNSMASQ_DST"

    echo "== installing photobooth-captive.service =="
    cp "$APP/deploy/photobooth-captive.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now photobooth-captive.service

    reactivate_ap
    echo
    echo "Captive portal ENABLED. One Wi-Fi QR now joins the booth AND opens the"
    echo "find-your-photos page automatically. Guests have no general internet while"
    echo "connected (expected). Test: join 'PhotoBooth' on a phone — the photos page"
    echo "should pop up on its own within a few seconds."
    ;;

  remove)
    echo "== removing captive portal =="
    systemctl disable --now photobooth-captive.service 2>/dev/null || true
    rm -f /etc/systemd/system/photobooth-captive.service
    rm -f "$DNSMASQ_DST"
    systemctl daemon-reload
    reactivate_ap
    echo "Captive portal REMOVED — guest hotspot restored to normal (pass-through) DNS."
    ;;

  status)
    echo "== service =="
    systemctl --no-pager status photobooth-captive.service 2>/dev/null | head -5 || \
      echo "not installed"
    echo "== dns hijack =="
    if [ -f "$DNSMASQ_DST" ]; then echo "present: $DNSMASQ_DST"; else echo "absent"; fi
    echo "== :80 listener =="
    ss -ltnp 2>/dev/null | grep ':80 ' || echo "nothing on :80"
    ;;

  *)
    echo "usage: $0 {install|remove|status}" >&2
    exit 1
    ;;
esac
