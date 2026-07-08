"""Face embedding engine.

Detection: MediaPipe Face Detection (CPU, fast). Embedding: ArcFace on the RK3588
NPU via RKNN (rknn-toolkit-lite2). `embed_image()` returns one L2-normalized vector
per detected face. Everything degrades gracefully: if rknnlite or the model is
missing, `available()` says so and the booth keeps working without face grouping.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .models import Settings

log = logging.getLogger("booth.faces")


class FaceEngine:
    name = "base"

    def available(self) -> tuple[bool, str]:
        return False, "not implemented"

    def embed_image(self, path) -> list:
        raise NotImplementedError


class NullFaceEngine(FaceEngine):
    name = "off"

    def available(self) -> tuple[bool, str]:
        return False, "face grouping disabled"

    def embed_image(self, path) -> list:
        return []


class RknnArcFaceEngine(FaceEngine):
    name = "rknn"

    def __init__(self, settings: Settings) -> None:
        self.model_path = settings.faces.rknn_model
        self.min_face = settings.faces.min_face_px
        self._rknn = None
        self._detector = None
        self._np = None
        self._cv2 = None

    def available(self) -> tuple[bool, str]:
        try:
            import cv2  # noqa
            import numpy  # noqa
        except Exception as e:
            return False, f"opencv/numpy missing: {e}"
        try:
            import mediapipe  # noqa
        except Exception as e:
            return False, f"mediapipe missing: {e}"
        try:
            from rknnlite.api import RKNNLite  # noqa
        except Exception as e:
            return False, f"rknnlite not installed: {e}"
        if not Path(self.model_path).exists():
            return False, f"model not found: {self.model_path}"
        return True, "ready"

    def _ensure(self) -> None:
        if self._rknn is not None:
            return
        import cv2
        import mediapipe as mp
        import numpy as np
        from rknnlite.api import RKNNLite

        self._np, self._cv2 = np, cv2
        # model_selection=0 = short-range (within ~2m) — best for booth-distance faces
        self._detector = mp.solutions.face_detection.FaceDetection(
            model_selection=0, min_detection_confidence=0.6)
        r = RKNNLite()
        if r.load_rknn(self.model_path) != 0:
            raise RuntimeError(f"load_rknn failed: {self.model_path}")
        if r.init_runtime() != 0:
            raise RuntimeError("rknn init_runtime failed")
        self._rknn = r
        log.info("ArcFace RKNN engine ready (%s)", self.model_path)

    def embed_image(self, path) -> list:
        self._ensure()
        np, cv2 = self._np, self._cv2
        img = cv2.imread(str(path))
        if img is None:
            return []
        h, w = img.shape[:2]
        res = self._detector.process(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        if not res.detections:
            return []
        out = []
        for det in res.detections:
            b = det.location_data.relative_bounding_box
            x1, y1 = max(0, int(b.xmin * w)), max(0, int(b.ymin * h))
            bw, bh = int(b.width * w), int(b.height * h)
            if bw < self.min_face or bh < self.min_face:
                continue
            face = img[y1:min(h, y1 + bh), x1:min(w, x1 + bw)]
            if face.size == 0:
                continue
            chip = cv2.resize(face, (112, 112))
            chip = cv2.cvtColor(chip, cv2.COLOR_BGR2RGB)
            # RKNN model is converted with mean/std baked in -> feed uint8 NHWC
            inp = np.expand_dims(chip, 0).astype(np.uint8)
            emb = self._rknn.inference(inputs=[inp])[0].flatten().astype("float32")
            n = float(np.linalg.norm(emb))
            if n > 0:
                emb = emb / n
            out.append(emb.tolist())
        return out


def make_face_engine(settings: Settings) -> FaceEngine:
    if not settings.faces.enabled or settings.faces.engine == "off":
        return NullFaceEngine()
    return RknnArcFaceEngine(settings)
