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
    # MODE=proxy (default): pass-through — guests keep the booth's internet, so phones
    #   stay connected and the selfie/download works in their real browser. Guests reach
    #   photos by scanning the on-screen QR. RECOMMENDED (avoids the captive mini-browser,
    #   which blocks the camera and drops no-internet Wi-Fi).
    # MODE=offline: DNS blackhole — no guest internet, but the find-your-photos page
    #   auto-opens on join. The in-portal selfie is unreliable on iOS.
    MODE="${2:-proxy}"
    if [ "$MODE" = "offline" ]; then
      echo "== offline mode: installing captive DNS hijack (no guest internet, auto-popup) =="
      mkdir -p "$DNSMASQ_DIR"
      cp "$APP/deploy/captive-dnsmasq.conf" "$DNSMASQ_DST"
    else
      echo "== proxy/pass-through mode: guests keep internet; reach photos via the QR =="
      rm -f "$DNSMASQ_DST"          # ensure no leftover DNS blackhole
    fi

    echo "== installing photobooth-captive.service =="
    cp "$APP/deploy/photobooth-captive.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now photobooth-captive.service

    reactivate_ap
    echo
    if [ "$MODE" = "offline" ]; then
      echo "Captive portal ENABLED (offline). Joining 'PhotoBooth' auto-opens the photos"
      echo "page, but guests have no internet and the in-portal selfie is unreliable on iOS."
    else
      echo "Captive portal ENABLED (pass-through). Guests keep internet and stay connected;"
      echo "they scan the on-screen photo QR to open their photos in their real browser —"
      echo "selfie + downloads work reliably. Re-run with 'install offline' for the"
      echo "no-internet auto-popup mode instead."
    fi
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
