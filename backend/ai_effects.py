"""AI background removal / replacement via portrait segmentation (rembg + onnxruntime).

Runs on the GPU when onnxruntime-gpu is present (CUDA/TensorRT EP), otherwise CPU —
same providers pattern as the face engine, so it auto-accelerates once the GPU wheel is
installed. Produces a soft alpha matte and composites the subject onto a solid colour or
a chosen backdrop image. Graceful: if rembg or the model is missing the photo passes
through untouched, so the booth never fails a capture over an effect.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from PIL import Image

from .models import Settings

log = logging.getLogger("booth.ai")

# rembg sessions are expensive to build (load an ONNX) — cache one per (model, gpu).
# Lock + single-entry cache for the same reasons as faces._APP_CACHE: no duplicate
# concurrent builds, and switching model/gpu from admin frees the old session's GPU
# memory instead of stacking a second one.
_SESS_CACHE: dict = {}
_SESS_LOCK = threading.Lock()


def available() -> tuple[bool, str]:
    try:
        import onnxruntime  # noqa
        import rembg  # noqa
    except Exception as e:
        return False, f"rembg/onnxruntime missing: {e}"
    return True, "ready"


def _hex_rgba(color: str) -> tuple:
    c = (color or "#ffffff").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except Exception:
        r, g, b = 255, 255, 255
    return (r, g, b, 255)


def _session(model_name: str, use_gpu: bool):
    key = (model_name, use_gpu)
    hit = _SESS_CACHE.get(key)
    if hit is not None:
        return hit
    with _SESS_LOCK:
        hit = _SESS_CACHE.get(key)          # double-check under the lock
        if hit is not None:
            return hit
        from rembg import new_session
        # CUDA EP does all the work here; the TensorRT EP was only ever second (so it never
        # got any nodes) yet still cost init time + GPU memory on boot — dropped.
        providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                     if use_gpu else ["CPUExecutionProvider"])
        sess = new_session(model_name, providers=providers)
        _SESS_CACHE.clear()                 # one session at a time (frees the old GPU model)
        _SESS_CACHE[key] = sess
        log.info("AI segmentation ready (model=%s, gpu=%s)", model_name, use_gpu)
        return sess


def warmup(settings: Settings) -> None:
    """Load the segmentation model at startup (and fetch it on first run) so the first
    capture isn't delayed by a cold init / model download."""
    if not settings.ai.enabled or settings.ai.effect == "none":
        return
    ok, _ = available()
    if not ok:
        return
    try:
        _session(settings.ai.model, settings.ai.use_gpu)
    except Exception as e:
        log.warning("ai warmup skipped: %s", e)


def apply_background_img(src_rgb: Image.Image, settings: Settings) -> Image.Image | None:
    """Remove/replace the background of an in-memory RGB image.

    Returns the new RGB image, or None when the effect is disabled, unavailable, or
    fails — so the caller keeps the original photo (a booth never fails a capture
    over an effect). In-memory so the capture pipeline decodes/encodes once."""
    ai = settings.ai
    if not ai.enabled or ai.effect == "none":
        return None
    ok, detail = available()
    if not ok:
        log.warning("ai effect '%s' requested but %s; skipping", ai.effect, detail)
        return None
    try:
        from rembg import remove
        sess = _session(ai.model or "u2net_human_seg", ai.use_gpu)
        cut = remove(src_rgb, session=sess)             # RGBA subject with alpha matte
        if cut.mode != "RGBA":
            cut = cut.convert("RGBA")

        if ai.effect == "bg_replace" and ai.background_image and Path(ai.background_image).exists():
            bg = Image.open(ai.background_image).convert("RGBA")
            # cover-fit the backdrop to the photo
            bg = _cover(bg, cut.size)
        else:
            bg = Image.new("RGBA", cut.size, _hex_rgba(ai.background_color))

        return Image.alpha_composite(bg, cut).convert("RGB")
    except Exception as e:
        log.warning("ai background effect failed: %s", e)
        return None


def apply_background(img_path: Path, settings: Settings) -> None:
    """Remove or replace the background of the image at img_path, in place (JPEG)."""
    out = apply_background_img(Image.open(img_path).convert("RGB"), settings)
    if out is not None:
        out.save(img_path, "JPEG", quality=92)


def _cover(img: Image.Image, size: tuple) -> Image.Image:
    """Resize+crop img to exactly cover `size` (like CSS object-fit: cover)."""
    tw, th = size
    iw, ih = img.size
    scale = max(tw / iw, th / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    img = img.resize((nw, nh))
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))
