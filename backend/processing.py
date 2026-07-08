"""Image post-processing: overlay frames/logos, multi-shot collages, AI effects (NPU hook)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from .models import Settings


def _load_rgba(path: str) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


def apply_overlay(img_path: Path, settings: Settings) -> None:
    """Composite a full-frame PNG and/or a logo onto an image, in place."""
    ov = settings.overlay
    if not ov.enabled:
        return
    base = Image.open(img_path).convert("RGBA")

    if ov.frame_png:
        frame = _load_rgba(ov.frame_png)
        if frame:
            frame = frame.resize(base.size)
            base = Image.alpha_composite(base, frame)

    if ov.logo_png:
        logo = _load_rgba(ov.logo_png)
        if logo:
            margin = int(base.width * 0.03)
            target_w = int(base.width * 0.18)
            ratio = target_w / logo.width
            logo = logo.resize((target_w, int(logo.height * ratio)))
            positions = {
                "tl": (margin, margin),
                "tr": (base.width - logo.width - margin, margin),
                "bl": (margin, base.height - logo.height - margin),
                "br": (base.width - logo.width - margin, base.height - logo.height - margin),
            }
            base.alpha_composite(logo, positions.get(ov.logo_position, positions["br"]))

    base.convert("RGB").save(img_path, "JPEG", quality=92)


def make_collage(paths: list[Path], settings: Settings, out_path: Path) -> Path:
    """Combine multiple shots into a single collage image."""
    c = settings.collage
    imgs = [Image.open(p).convert("RGB") for p in paths]
    if not imgs:
        raise ValueError("no images for collage")
    gap = c.gap_px
    bg = c.background

    if c.layout == "grid_2x2":
        cols, rows = 2, 2
    elif c.layout == "strip_horizontal":
        cols, rows = len(imgs), 1
    else:  # strip_vertical
        cols, rows = 1, len(imgs)

    # normalize cell size to the first image's aspect
    cw, ch = imgs[0].size
    scale = 600 / cw
    cw, ch = int(cw * scale), int(ch * scale)
    cells = [im.resize((cw, ch)) for im in imgs]

    width = cols * cw + (cols + 1) * gap
    height = rows * ch + (rows + 1) * gap
    canvas = Image.new("RGB", (width, height), bg)
    for idx, cell in enumerate(cells[: cols * rows]):
        r, col = divmod(idx, cols)
        x = gap + col * (cw + gap)
        y = gap + r * (ch + gap)
        canvas.paste(cell, (x, y))
    canvas.save(out_path, "JPEG", quality=92)
    return out_path


def apply_ai(img_path: Path, settings: Settings) -> None:
    """On-device AI effect via the RK3588 NPU (RKNN). No-op until a model is set.

    Wiring point for background removal/replacement. Convert a segmentation model
    to .rknn with rknn-toolkit2 on a PC, run it here with rknn-toolkit-lite2.
    """
    ai = settings.ai
    if not ai.enabled or ai.effect == "none":
        return
    model = ai.rknn_model
    if not model or not Path(model).exists():
        print(f"[ai] effect '{ai.effect}' requested but no RKNN model at '{model}'; skipping")
        return
    try:
        # Placeholder for the real NPU pipeline:
        #   from rknnlite.api import RKNNLite
        #   rknn = RKNNLite(); rknn.load_rknn(model); rknn.init_runtime()
        #   ... infer mask, composite background ...
        print(f"[ai] (stub) would apply '{ai.effect}' using {model} to {img_path}")
    except Exception as e:
        print(f"[ai] effect failed: {e}")
