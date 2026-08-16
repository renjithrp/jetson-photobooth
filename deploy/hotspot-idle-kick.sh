#!/bin/bash
# Deauth guest devices that have been idle on the booth hotspot for 5+ minutes
# (runs every minute via hotspot-idle-kick.timer). With the full captive hijack,
# a lingering phone has no internet — kicking it returns the phone to LTE/home
# Wi-Fi and frees 2.4 GHz airtime and client slots on the USB AP dongle.
#
# Booth-owned devices are never kicked: list their MACs (one per line, lowercase)
# in /etc/photobooth-idle-exempt (kiosk iPad, camera, staff devices).
IFACE="${BOOTH_AP_IFACE:-wlx782051871b2c}"
IDLE_MS=$((300 * 1000))
EXEMPT_FILE=/etc/photobooth-idle-exempt

exempt=""
[ -f "$EXEMPT_FILE" ] && exempt=$(tr "A-Z" "a-z" < "$EXEMPT_FILE")

iw dev "$IFACE" station dump 2>/dev/null | awk -v idle="$IDLE_MS" '
    /^Station/        { mac = tolower($2) }
    /inactive time:/  { if ($3 + 0 > idle) print mac }
' | while read -r mac; do
    if echo "$exempt" | grep -q "$mac"; then
        continue
    fi
    logger -t hotspot-idle-kick "deauthing idle guest $mac (>5 min)"
    iw dev "$IFACE" station del "$mac"
done
exit 0
