#!/usr/bin/env bash
# Turn a USB Wi-Fi dongle into a guest "PhotoBooth" hotspot so visitors can connect
# and download their photos with NO cloud and NO venue Wi-Fi. The Pi's onboard Wi-Fi
# (wlan0) stays as your admin/management link — this only touches the dongle.
#
# Uses NetworkManager (nmcli): `ipv4.method shared` auto-provides DHCP + NAT for guests.
#
#   ./setup-hotspot.sh                # auto-detect the dongle, default SSID/pass
#   ./setup-hotspot.sh up   wlan1 "PhotoBooth" "booth1234"
#   ./setup-hotspot.sh down           # tear the hotspot down
#   ./setup-hotspot.sh status
#
# After `up`, set the QR/share base URL to the hotspot so guest QR codes resolve on
# the hotspot network (the script offers to do this for you).
set -euo pipefail

ACTION="${1:-up}"
CON=photobooth-ap
AP_IP=192.168.50.1
SSID="${3:-PhotoBooth}"
PASS="${4:-booth1234}"          # >=8 chars for WPA2; change for your event

MGMT_IFACE=wlan0                # onboard Wi-Fi — never touched, this is your admin link

detect_iface() {
  # the Wi-Fi device that is NOT the onboard management interface = the dongle
  nmcli -t -f DEVICE,TYPE device 2>/dev/null \
    | awk -F: -v m="$MGMT_IFACE" '$2=="wifi" && $1!=m && $1!="" {print $1; exit}'
}

case "$ACTION" in
  up)
    IFACE="${2:-$(detect_iface)}"
    if [ -z "${IFACE:-}" ]; then
      echo "No second Wi-Fi interface found. Plug in the USB dongle and re-run."
      echo "Detected Wi-Fi devices:"; nmcli -t -f DEVICE,TYPE device | grep wifi || true
      exit 1
    fi
    if [ "$IFACE" = "$MGMT_IFACE" ]; then
      echo "Refusing to use the onboard $MGMT_IFACE (that's your admin link)." >&2
      echo "Pass the dongle interface explicitly: ./setup-hotspot.sh up wlanX" >&2
      exit 1
    fi
    echo "== creating hotspot '$SSID' on $IFACE ($AP_IP) =="
    nmcli connection delete "$CON" 2>/dev/null || true
    nmcli connection add type wifi ifname "$IFACE" con-name "$CON" autoconnect yes \
      ssid "$SSID" \
      mode ap \
      802-11-wireless.band bg \
      802-11-wireless.hidden yes \
      ipv4.method shared ipv4.addresses "$AP_IP/24" \
      ipv6.method disabled \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS"
    nmcli connection up "$CON"
    echo
    echo "Hotspot is UP:  SSID='$SSID' (HIDDEN)  password='$PASS'  booth=http://$AP_IP:8000"
    echo "SSID is hidden -> guests join by scanning the kiosk Wi-Fi QR (it carries H:true)."
    echo "Guests: scan join QR -> scan photo QR -> download."
    echo
    echo "To make QR codes point at the hotspot:"
    echo "  1) add the hotspot IP to the TLS cert:   ./gen-cert.sh $AP_IP && systemctl restart photobooth"
    echo "  2) set the share base URL:               ./setup-hotspot.sh baseurl"
    echo "  (guests get a one-time 'not secure' notice — self-signed cert; tap through to download)"
    ;;

  baseurl)
    # set share.base_url to the hotspot so generated QR codes resolve for guests.
    # Backend is HTTPS-only, so the QR must be https:// (ensure the cert includes $AP_IP).
    PIN="${2:-1234}"
    JAR=$(mktemp)
    curl -sk -c "$JAR" -X POST https://localhost:8000/api/login \
      -H 'Content-Type: application/json' -d "{\"pin\":\"$PIN\"}" >/dev/null
    curl -sk -b "$JAR" -X PUT https://localhost:8000/api/settings \
      -H 'Content-Type: application/json' \
      -d "{\"share\":{\"base_url\":\"https://$AP_IP:8000\"}}" -o /dev/null -w "set base_url -> %{http_code}\n"
    rm -f "$JAR"
    echo "QR codes now point to https://$AP_IP:8000 (reachable on the '$SSID' hotspot)."
    ;;

  down)
    nmcli connection down "$CON" 2>/dev/null || true
    nmcli connection delete "$CON" 2>/dev/null || true
    echo "Hotspot '$CON' removed. Onboard $MGMT_IFACE untouched."
    ;;

  status)
    echo "== Wi-Fi devices =="; nmcli -t -f DEVICE,TYPE,STATE device | grep wifi || true
    echo "== hotspot connection =="; nmcli -t connection show "$CON" 2>/dev/null | grep -E "connection.id|GENERAL.STATE|ipv4.addresses|802-11-wireless.ssid" || echo "(not configured)"
    ;;

  *)
    echo "usage: $0 {up [iface] [ssid] [pass] | down | status | baseurl [pin]}"; exit 1;;
esac
