#!/usr/bin/env bash
# Install the on-Pi pieces for face grouping:
#   - OpenCV + MediaPipe (CPU face detection)
#   - upgrade librknnrt.so (the shipped one is old: v1.4.0) + matching rknn-toolkit-lite2
# Run on the Pi.  IMPORTANT: the converter on the x86 host (rknn-toolkit2) MUST be the
# SAME version (RKNN_VER below) as the runtime/lite installed here.
set -euo pipefail
PIP=/opt/photobooth/venv/bin/pip
RKNN_VER="${RKNN_VER:-2.3.2}"          # use one version everywhere (toolkit2 == lite == librknnrt)
REPO="https://github.com/airockchip/rknn-toolkit2/raw/v${RKNN_VER}"

echo "== OpenCV + MediaPipe (detection) =="
$PIP install -q opencv-python-headless mediapipe "numpy<2" || \
  echo "(MediaPipe may need a custom aarch64 wheel on some images)"

echo "== upgrade NPU runtime librknnrt.so -> v${RKNN_VER} =="
if curl -fsSL "$REPO/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so" -o /tmp/librknnrt.so; then
  cp -a /usr/lib/librknnrt.so "/usr/lib/librknnrt.so.bak.$(date +%s)" 2>/dev/null || true
  install -m 0644 /tmp/librknnrt.so /usr/lib/librknnrt.so
  ldconfig
  echo "  installed /usr/lib/librknnrt.so (backup kept)"
else
  echo "  WARN: could not fetch librknnrt.so for v${RKNN_VER}; keeping the existing one."
  echo "  Then you MUST set RKNN_VER to match the existing runtime (currently ~1.4.0)."
fi

echo "== rknn-toolkit-lite2 v${RKNN_VER} (NPU python runtime) =="
WHL="rknn_toolkit_lite2-${RKNN_VER}-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl"
if ! $PIP install "$REPO/rknn-toolkit-lite2/packages/${WHL}"; then
  echo "  Auto-install failed — download a cp310/aarch64 wheel matching v${RKNN_VER} from"
  echo "  https://github.com/airockchip/rknn-toolkit2/tree/v${RKNN_VER}/rknn-toolkit-lite2/packages"
  echo "  and: $PIP install <wheel>"
  exit 1
fi

mkdir -p /opt/photobooth/models
echo
echo "Done. Next:"
echo "  1) On an x86 host: pip install rknn-toolkit2==${RKNN_VER}"
echo "     python native/convert_arcface_to_rknn.py w600k_mbf.onnx arcface.rknn"
echo "  2) scp arcface.rknn -> /opt/photobooth/models/arcface.rknn"
echo "  3) Admin -> Face grouping -> Enabled -> Save"
