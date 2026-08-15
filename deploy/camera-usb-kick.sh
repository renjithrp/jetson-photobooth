#!/bin/bash
# Boot-time guard for the Sony camera's USB link (runs via camera-usb-kick.service).
#
# WHY: after a Jetson reboot the A7R IV can be left holding a stale PC-Remote
# session — VBUS on the USB-C port never drops during the restart, the camera
# never sees a disconnect, and so it never re-announces itself. Result: zero
# attach events since boot, cameraDaemon crash-looping on "no camera" until a
# human replugs the cable. Resetting the fusb301 Type-C controller forces the
# same CC renegotiation as a physical replug (verified on the booth 2026-08-15:
# freset -> instant disconnect + SuperSpeed re-enumeration of the ILCE-7RM4A).
#
# Exits 0 always — a missing camera (powered off) must not fail the boot.

FRESET=/sys/bus/i2c/devices/1-0025/fusb301/freset

sleep 8   # let normal boot-time USB enumeration finish first

for i in 1 2 3; do
    if lsusb -d 054c: >/dev/null; then
        echo "Sony camera present on USB"
        exit 0
    fi
    if [ ! -w "$FRESET" ]; then
        echo "fusb301 freset not available at $FRESET; nothing to kick"
        exit 0
    fi
    echo "camera missing -> resetting Type-C controller (attempt $i)"
    echo 1 > "$FRESET"
    sleep 6
done

if lsusb -d 054c: >/dev/null; then
    echo "Sony camera present on USB (after reset)"
else
    echo "camera still missing after Type-C resets — is it powered on?"
fi
exit 0
