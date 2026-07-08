"""Face embedding engine.

On the Jetson we use **InsightFace** (SCRFD detector + ArcFace r50 embedding) running on
the GPU via onnxruntime (CUDA / TensorRT execution provider), with an automatic CPU
fallback. `embed_image()` returns one L2-normalized 512-d vector per detected face.

Everything degrades gracefully: if onnxruntime/insightface or the model pack is missing,
`available()` says so and the booth keeps working without face grouping. Embeddings are
engine-agnostic, so `face_index` clustering is unchanged.
"""
from __future__ import annotations

import logging

from . import config
from .models import Settings

log = logging.getLogger("booth.faces")

# Loading the InsightFace pack (5 ONNX models) costs tens of seconds, so keep ONE loaded
# instance per (pack, det_size, gpu) config and share it across every engine instance —
# make_face_engine() is called per capture session, and we must not reload each time.
_APP_CACHE: dict = {}


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
    # models live under <root>/models/<pack>/ — pin to the app dir so it works offline
    # regardless of which user runs the service.
    root = str(config.data_dir().parent)
    # only load what grouping needs (detector + ArcFace embedding); skip the landmark and
    # genderage models to cut load time + memory.
    app = FaceAnalysis(name=model_pack, root=root, providers=providers,
                       allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=ctx_id, det_size=(det_size, det_size))
    try:
        in_use = list(app.models["recognition"].session.get_providers())
    except Exception:
        in_use = providers
    log.info("InsightFace loaded (pack=%s, det=%d, providers=%s)", model_pack, det_size, in_use)
    _APP_CACHE[key] = (app, in_use)
    return _APP_CACHE[key]


def active_providers() -> list[str]:
    """The execution providers of the currently-loaded face model (for admin status)."""
    for _app, prov in _APP_CACHE.values():
        return prov
    return []


def warmup(settings: Settings) -> None:
    """Load the face model at startup so the first guest doesn't wait for a cold init."""
    if not settings.faces.enabled or settings.faces.engine == "off":
        return
    try:
        _get_app(settings.faces.model_pack, settings.faces.det_size, settings.faces.use_gpu)
    except Exception as e:
        log.warning("face warmup skipped: %s", e)


class FaceEngine:
    name = "base"

    def available(self) -> tuple[bool, str]:
        return False, "not implemented"

    def embed_image(self, path) -> list:
        raise NotImplementedError

    def providers(self) -> list[str]:
        return []


class NullFaceEngine(FaceEngine):
    name = "off"

    def available(self) -> tuple[bool, str]:
        return False, "face grouping disabled"

    def embed_image(self, path) -> list:
        return []


class InsightFaceEngine(FaceEngine):
    name = "insightface"

    def __init__(self, settings: Settings) -> None:
        f = settings.faces
        self.model_pack = f.model_pack
        self.det_size = f.det_size
        self.use_gpu = f.use_gpu
        self.min_face = f.min_face_px
        self._app = None
        self._providers: list[str] = []

    def available(self) -> tuple[bool, str]:
        try:
            import cv2  # noqa
            import numpy  # noqa
        except Exception as e:
            return False, f"opencv/numpy missing: {e}"
        try:
            import onnxruntime  # noqa
        except Exception as e:
            return False, f"onnxruntime missing: {e}"
        try:
            import insightface  # noqa
        except Exception as e:
            return False, f"insightface missing: {e}"
        return True, "ready"

    def _ensure(self) -> None:
        if self._app is not None:
            return
        self._app, self._providers = _get_app(self.model_pack, self.det_size, self.use_gpu)

    def providers(self) -> list[str]:
        return self._providers

    def embed_image(self, path) -> list:
        self._ensure()
        import cv2

        img = cv2.imread(str(path))
        if img is None:
            return []
        faces = self._app.get(img)
        out = []
        for fc in faces:
            x1, y1, x2, y2 = [int(v) for v in fc.bbox]
            if (x2 - x1) < self.min_face or (y2 - y1) < self.min_face:
                continue
            emb = fc.normed_embedding  # already L2-normalized, 512-d
            out.append(emb.astype("float32").tolist())
        return out


def make_face_engine(settings: Settings) -> FaceEngine:
    if not settings.faces.enabled or settings.faces.engine == "off":
        return NullFaceEngine()
    return InsightFaceEngine(settings)
