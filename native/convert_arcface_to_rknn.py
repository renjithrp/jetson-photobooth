#!/usr/bin/env python3
"""Convert an ArcFace ONNX face-recognition model to RKNN for the RK3588 NPU.

Run on an x86-64 Linux host (NOT the Pi) with rknn-toolkit2 installed:
    pip install rknn-toolkit2            # Rockchip's converter (x86 only)

Get an ArcFace ONNX with 112x112 RGB input, e.g. from InsightFace
(`w600k_mbf.onnx` / MobileFaceNet is small & fast on the NPU; `w600k_r50` is more
accurate). Then:
    python convert_arcface_to_rknn.py w600k_mbf.onnx arcface.rknn
    # optional INT8 quantization (faster) needs a calibration list of face crops:
    python convert_arcface_to_rknn.py w600k_mbf.onnx arcface.rknn calib.txt

Copy the resulting arcface.rknn to the Pi at the path set in Admin → Face grouping
(default /opt/photobooth/models/arcface.rknn), then enable face grouping.
"""
import sys

from rknn.api import RKNN   # provided by rknn-toolkit2 (x86)


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    onnx, out = sys.argv[1], sys.argv[2]
    calib = sys.argv[3] if len(sys.argv) > 3 else None

    rknn = RKNN(verbose=True)
    # ArcFace preprocessing: RGB, normalize (x-127.5)/128. Baking mean/std in lets the
    # Pi feed raw uint8 NHWC frames (see backend/faces.py).
    rknn.config(mean_values=[[127.5, 127.5, 127.5]],
                std_values=[[127.5, 127.5, 127.5]],   # InsightFace ArcFace: (x-127.5)/127.5
                target_platform="rk3588")
    # ArcFace ONNX often has a dynamic batch dim -> pin it to 1x3x112x112
    import onnx as _onnx
    inp_name = _onnx.load(onnx).graph.input[0].name
    assert rknn.load_onnx(model=onnx, inputs=[inp_name],
                          input_size_list=[[1, 3, 112, 112]]) == 0, "load_onnx failed"
    assert rknn.build(do_quantization=bool(calib), dataset=calib) == 0, "build failed"
    assert rknn.export_rknn(out) == 0, "export failed"
    print(f"\nWrote {out}  (do_quantization={bool(calib)})")
    rknn.release()


if __name__ == "__main__":
    main()
