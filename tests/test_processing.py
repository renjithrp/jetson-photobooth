"""In-memory image transforms for the single-decode/single-encode capture pipeline."""
from PIL import Image

from backend import processing
from backend.models import Settings


def _photo(color=(120, 120, 120)):
    return Image.new("RGB", (400, 300), color)


def test_overlay_disabled_returns_none():
    s = Settings()                                  # overlay.enabled defaults False
    assert processing.apply_overlay_img(_photo(), s) is None


def test_overlay_frame_composites(tmp_path):
    # a semi-transparent red frame must visibly change the photo, in memory
    frame = Image.new("RGBA", (10, 10), (255, 0, 0, 200))
    fp = tmp_path / "frame.png"
    frame.save(fp)
    s = Settings()
    s.overlay.enabled = True
    s.overlay.frame_png = str(fp)
    base = _photo()
    out = processing.apply_overlay_img(base, s)
    assert out is not None
    assert out.size == base.size and out.mode == "RGB"
    assert out.tobytes() != base.tobytes()          # pixels actually changed


def test_ai_disabled_returns_none():
    s = Settings()                                  # ai.enabled defaults False
    assert processing.apply_ai_img(_photo(), s) is None


def test_pipeline_leaves_untouched_shot_bytes_identical(tmp_path):
    # With no effects enabled the capture pipeline must NOT re-encode the shot —
    # verify the transform layer signals "no change" (returns None) for both stages,
    # which is what lets capture_service skip the save.
    s = Settings()
    img = _photo()
    assert processing.apply_overlay_img(img, s) is None
    assert processing.apply_ai_img(img, s) is None
