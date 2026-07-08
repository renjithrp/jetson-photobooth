#!/usr/bin/env bash
# Build the Sony CrSDK helpers on the Pi:
#   boothCapture   - capture a shot and download to the Pi
#   liveviewServer - serve the camera live view as MJPEG on :8080 (non-interactive)
# Expects the CrSDK extracted at $CRSDK (default /root/CrSDK) with app/ + external/crsdk/.
set -euo pipefail
CRSDK="${CRSDK:-/root/CrSDK}"
HERE="$(cd "$(dirname "$0")" && pwd)"

build() {
  local src="$1" out="$2"
  cp "$HERE/$src" "$CRSDK/app/$src"
  cd "$CRSDK"
  g++ -std=c++17 -fsigned-char -fstack-protector-all \
      "app/$src" app/CrDebugString.cpp \
      -Iapp -Iapp/CRSDK -Lexternal/crsdk -lCr_Core -lpthread \
      -Wl,-rpath,'$ORIGIN' -o "external/crsdk/$out"
  echo "Built: $CRSDK/external/crsdk/$out"
}

build boothCapture.cpp   boothCapture
build liveviewServer.cpp liveviewServer
build cameraDaemon.cpp   cameraDaemon
