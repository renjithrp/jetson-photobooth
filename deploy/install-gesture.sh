#!/usr/bin/env bash
# Install the hand-gesture trigger as an ISOLATED MediaPipe worker.
#
# MediaPipe requires numpy<2; the booth's GPU AI stack (rembg, opencv 5.0) requires
# numpy>=2. They cannot coexist in one venv, so MediaPipe goes in its own venv and the
# gesture detector runs out-of-process (backend/gesture_worker.py), reading the backend's
# buffered live-view and firing /api/capture. This NEVER touches /opt/photobooth/venv.
#
# Usage on the Jetson:
#     sudo bash /opt/photobooth/deploy/install-gesture.sh
set -uo pipefail

APP="${APP:-/opt/photobooth}"
GVENV="$APP/gesture-venv"
SVC=photobooth-gesture.service
RUN_USER="${RUN_USER:-pb}"

[ -f "$APP/backend/gesture_worker.py" ] || { echo "ERROR: $APP/backend/gesture_worker.py missing (sync the repo first)"; exit 1; }

echo "== 1) create isolated gesture venv: $GVENV =="
if [ ! -x "$GVENV/bin/python" ]; then
  python3 -m venv "$GVENV" || { echo "venv creation failed"; exit 1; }
fi
"$GVENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true

echo "== 2) install MediaPipe + OpenCV into the isolated venv (numpy<2 lives HERE only) =="
"$GVENV/bin/pip" install --no-input mediapipe || {
  echo "!! MediaPipe has no installable wheel for this platform — cannot enable gesture."
  echo "   Use the Arduino button instead (admin: Trigger mode = arduino/both)."
  exit 2; }

echo "== 3) verify the isolated venv =="
"$GVENV/bin/python" - <<'PY'
import importlib, sys
rc = 0
for m in ("numpy", "cv2", "mediapipe"):
    try:
        mod = importlib.import_module(m); print(f"  OK   {m} {getattr(mod,'__version__','?')}")
    except Exception as e:
        rc = 1; print(f"  FAIL {m}: {e}")
try:
    import mediapipe as mp; mp.solutions.hands.Hands; print("  OK   mediapipe.solutions.hands")
except Exception as e:
    rc = 1; print(f"  FAIL mediapipe.solutions.hands: {e}")
sys.exit(rc)
PY
[ $? -ne 0 ] && { echo "gesture venv verification failed"; exit 3; }

echo "== 4) confirm the MAIN venv is still on numpy 2 (untouched) =="
"$APP/venv/bin/python" -c "import numpy,onnxruntime; print('  main venv numpy', numpy.__version__, '| onnxruntime', onnxruntime.__version__)" || \
  echo "  WARN: could not verify main venv"

echo "== 5) install + enable the systemd service =="
install -m 0644 "$APP/deploy/$SVC" "/etc/systemd/system/$SVC"
systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1 || true
systemctl restart "$SVC"
sleep 3
echo
systemctl --no-pager --lines=0 status "$SVC" | head -n 5 || true
echo
echo "== recent worker log =="
journalctl -u "$SVC" -n 15 --no-pager

echo
echo "DONE. Make the configured gesture at the camera; watch it live with:"
echo "    journalctl -u $SVC -f"
