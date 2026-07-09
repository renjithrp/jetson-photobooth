"""Gaze correction (eye redirection) — STEP 1-2 SCAFFOLD: measurement only.

This module is the foundation for a "make the subject look at the camera" effect. Right
now it does NOT modify photos. On each captured shot it:
  1. detects faces + 106-pt landmarks + head pose via InsightFace (GPU/CPU),
  2. estimates each face's head yaw/pitch and eye-openness,
  3. applies the safety GATE (near-frontal + eyes open) from GazeSettings, and
  4. LOGS how often a correction *would* fire — cumulatively, so you can judge whether
     the feature is worth the model work before building it.

The actual redirection (phase 3) will run a flow-warp ONNX model on each eye crop and
honour `settings.gaze.strength`; it slots in where `_would_correct` is computed below.

Graceful like ai_effects: if onnxruntime/insightface or the model is missing, the shot
passes through untouched — the booth never fails a capture over this.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import config
from .models import Settings

log = logging.getLogger("booth.gaze")

# One InsightFace app per (pack, det_size, gpu) — loading the ONNX pack is expensive.
# Separate cache from faces.py because we need the landmark + pose modules it skips.
_APP_CACHE: dict = {}

# Cumulative measurement counters (process lifetime) so the log shows a running trigger
# rate, not just per-image noise. Reset on restart.
_STATS = {"images": 0, "faces": 0, "would_correct": 0}

# InsightFace 106-point landmark convention: the two eye clusters. Guarded at use — if a
# pack returns a different layout the openness check is skipped (eyes treated as open).
_LEFT_EYE = slice(33, 43)
_RIGHT_EYE = slice(87, 97)


def available() -> tuple[bool, str]:
    try:
        import onnxruntime  # noqa
        import insightface  # noqa
        import cv2  # noqa
    except Exception as e:
        return False, f"insightface/onnxruntime/cv2 missing: {e}"
    return True, "ready"


def _get_app(model_pack: str, det_size: int, use_gpu: bool):
    key = (model_pack, det_size, use_gpu)
    hit = _APP_CACHE.get(key)
    if hit is not None:
        return hit
    from insightface.app import FaceAnalysis

    if use_gpu:
        providers = ["CUDAExecutionProvider", "TensorrtExecutionProvider", "CPUExecutionProvider"]
        ctx_id = 0
    else:
        providers = ["CPUExecutionProvider"]
        ctx_id = -1
    root = str(config.data_dir().parent)   # models under <root>/models/<pack>/, offline-safe
    # detection + landmarks + head pose; recognition/genderage not needed for gaze.
    app = FaceAnalysis(name=model_pack, root=root, providers=providers,
                       allowed_modules=["detection", "landmark_2d_106", "landmark_3d_68"])
    app.prepare(ctx_id=ctx_id, det_size=(det_size, det_size))
    log.info("gaze detector loaded (pack=%s, det=%d, gpu=%s)", model_pack, det_size, use_gpu)
    _APP_CACHE[key] = app
    return app


def warmup(settings: Settings) -> None:
    """Load the detector at startup so the first capture isn't delayed by a cold init."""
    if not settings.gaze.enabled:
        return
    ok, _ = available()
    if not ok:
        return
    try:
        _get_app(settings.faces.model_pack, settings.faces.det_size, settings.gaze.use_gpu)
    except Exception as e:
        log.warning("gaze warmup skipped: %s", e)


def _eye_openness(landmarks, eye: slice) -> float:
    """EAR-like proxy: vertical span / horizontal span of an eye's landmark cluster.
    Open eye ~0.3-0.5, closed <~0.15. Returns a large value (treat as open) if the
    landmarks aren't in the expected layout, so a missing/odd pack never false-blocks."""
    try:
        pts = landmarks[eye]
        if len(pts) < 4:
            return 1.0
        xs = pts[:, 0]
        ys = pts[:, 1]
        w = float(xs.max() - xs.min())
        h = float(ys.max() - ys.min())
        return h / (w + 1e-6)
    except Exception:
        return 1.0


def _head_angles(face) -> tuple[float, float]:
    """(abs yaw, abs pitch) in degrees from InsightFace's pose, or (0,0) if unavailable
    so a face with no pose still counts as near-frontal rather than being dropped."""
    pose = getattr(face, "pose", None)
    if pose is None or len(pose) < 2:
        return 0.0, 0.0
    pitch, yaw = float(pose[0]), float(pose[1])
    return abs(yaw), abs(pitch)


def apply_gaze(img_path: Path, settings: Settings) -> None:
    """SCAFFOLD: measure + log whether gaze correction would fire; leave the photo intact.

    Phase 3 will, for each face where `would_correct` is True, run the redirection model
    on the eye crops and composite the result back — right here, in place."""
    g = settings.gaze
    if not g.enabled:
        return
    ok, detail = available()
    if not ok:
        log.warning("gaze requested but %s; skipping", detail)
        return
    try:
        import cv2
        app = _get_app(settings.faces.model_pack, settings.faces.det_size, g.use_gpu)
        img = cv2.imread(str(img_path))
        if img is None:
            log.warning("gaze: could not read %s", img_path.name)
            return
        faces = app.get(img)

        _STATS["images"] += 1
        n_correct = 0
        for f in faces:
            yaw, pitch = _head_angles(f)
            lm = getattr(f, "landmark_2d_106", None)
            if lm is not None:
                open_l = _eye_openness(lm, _LEFT_EYE)
                open_r = _eye_openness(lm, _RIGHT_EYE)
            else:
                open_l = open_r = 1.0
            eyes_open = min(open_l, open_r) >= g.min_eye_openness
            frontal = yaw <= g.max_head_angle and pitch <= g.max_head_angle
            would = frontal and eyes_open
            n_correct += int(would)
            log.info("gaze face: yaw=%.0f pitch=%.0f openL=%.2f openR=%.2f "
                     "frontal=%s eyes_open=%s -> would_correct=%s",
                     yaw, pitch, open_l, open_r, frontal, eyes_open, would)

        _STATS["faces"] += len(faces)
        _STATS["would_correct"] += n_correct
        rate = (100.0 * _STATS["would_correct"] / _STATS["faces"]) if _STATS["faces"] else 0.0
        log.info("gaze %s: %d face(s), %d would-correct | cumulative: %d/%d faces (%.0f%%) "
                 "over %d images [MEASURE ONLY — photo unchanged]",
                 img_path.name, len(faces), n_correct,
                 _STATS["would_correct"], _STATS["faces"], rate, _STATS["images"])
    except Exception as e:
        log.warning("gaze measurement failed on %s: %s", img_path.name, e)


def get_stats() -> dict:
    """Cumulative measurement counters (for a future admin readout / status endpoint)."""
    faces = _STATS["faces"]
    return {**_STATS,
            "would_correct_pct": round(100.0 * _STATS["would_correct"] / faces, 1) if faces else 0.0}
