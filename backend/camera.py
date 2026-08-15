"""Camera backends.

- MockCamera   : generates JPEGs with Pillow so the whole app runs with no hardware.
- SonyCamera   : drives the CrSDK `boothCapture` binary on the Pi (captures + downloads).
- WebcamCamera : grabs a frame from a USB webcam via OpenCV (optional dependency).

All backends implement capture(dest_dir, count, on_shot) -> list[Path].
"""
from __future__ import annotations

import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from .models import Settings


class CaptureError(Exception):
    pass


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


class CameraBackend:
    name = "base"

    def status(self) -> dict:
        return {"backend": self.name, "ok": True, "detail": ""}

    def capture(self, dest_dir: Path, count: int,
                on_shot: Callable[[int], None] | None = None) -> list[Path]:
        raise NotImplementedError


class MockCamera(CameraBackend):
    name = "mock"

    def __init__(self, booth_name: str = "AI Photo Booth") -> None:
        self.booth_name = booth_name

    def capture(self, dest_dir, count, on_shot=None) -> list[Path]:
        out: list[Path] = []
        palette = [(255, 209, 220), (191, 228, 255), (210, 255, 214),
                   (255, 243, 191), (226, 209, 255)]
        for i in range(count):
            if on_shot:
                on_shot(i + 1)
            bg = palette[i % len(palette)]
            img = Image.new("RGB", (1280, 853), bg)
            d = ImageDraw.Draw(img)
            d.ellipse((540, 250, 740, 450), fill=(255, 255, 255))
            d.ellipse((585, 320, 615, 350), fill=(40, 40, 40))
            d.ellipse((665, 320, 695, 350), fill=(40, 40, 40))
            d.arc((585, 330, 695, 420), start=20, end=160, fill=(40, 40, 40), width=8)
            d.text((40, 30), self.booth_name, fill=(30, 30, 30))
            d.text((40, 790), f"MOCK  shot {i+1}/{count}  {datetime.now():%H:%M:%S}",
                   fill=(30, 30, 30))
            p = dest_dir / f"shot_{i+1:02d}_{_ts()}.jpg"
            img.save(p, "JPEG", quality=90)
            out.append(p)
            time.sleep(0.05)
        return out


class WebcamCamera(CameraBackend):
    name = "webcam"

    def __init__(self, index: int = 0) -> None:
        self.index = index

    def capture(self, dest_dir, count, on_shot=None) -> list[Path]:
        try:
            import cv2  # noqa
        except Exception as e:
            raise CaptureError(f"opencv not installed for webcam capture: {e}")
        cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            raise CaptureError(f"cannot open webcam index {self.index}")
        out: list[Path] = []
        try:
            for i in range(count):
                if on_shot:
                    on_shot(i + 1)
                ok, frame = cap.read()
                if not ok:
                    raise CaptureError("webcam read failed")
                p = dest_dir / f"shot_{i+1:02d}_{_ts()}.jpg"
                cv2.imwrite(str(p), frame)
                out.append(p)
                time.sleep(0.2)
        finally:
            cap.release()
        return out


class SonyCamera(CameraBackend):
    """Runs the CrSDK helper binary; it downloads each shot into output_dir.

    NOTE: full-resolution USB transfer on the A7R IV can drop the link; the
    booth defaults to the 'small' transfer size set inside boothCapture.
    """
    name = "sony"

    def __init__(self, settings: Settings) -> None:
        c = settings.camera
        self.binary = Path(c.capture_binary)
        self.output_dir = Path(c.capture_output_dir)
        self.timeout = c.capture_timeout_seconds

    def status(self) -> dict:
        ok = self.binary.exists()
        return {"backend": self.name, "ok": ok,
                "detail": "" if ok else f"binary not found: {self.binary}"}

    def _existing(self) -> set[str]:
        if not self.output_dir.exists():
            return set()
        return {p.name for p in self.output_dir.iterdir() if p.is_file()}

    def capture(self, dest_dir, count, on_shot=None) -> list[Path]:
        if not self.binary.exists():
            raise CaptureError(f"capture binary missing: {self.binary}")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for i in range(count):
            if on_shot:
                on_shot(i + 1)
            before = self._existing()
            try:
                # run from the binary's dir so libCr_Core.so + CrAdapter resolve
                subprocess.run([str(self.binary)], cwd=str(self.binary.parent),
                               timeout=self.timeout, capture_output=True)
            except subprocess.TimeoutExpired:
                raise CaptureError("camera capture timed out (USB transfer?)")
            new = sorted(self._existing() - before)
            if not new:
                raise CaptureError("no image downloaded from camera")
            for name in new:
                src = self.output_dir / name
                dst = dest_dir / f"shot_{i+1:02d}_{_ts()}{src.suffix}"
                shutil.move(str(src), str(dst))
                out.append(dst)
        return out


def daemon_capture(settings: Settings, dest_dir: Path, count: int,
                   on_shot: Callable[[int], None] | None = None) -> list[Path]:
    """Capture via the unified camera daemon's /capture endpoint (one camera session
    shared with live view — no stop/start). The daemon does AF + shutter + download
    into capture_output_dir; we move the new file(s) into the session dir."""
    import json
    import urllib.request

    outdir = Path(settings.camera.capture_output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = (settings.preview.sony_http_url or "http://127.0.0.1:8080/").rstrip("/")
    out: list[Path] = []
    for i in range(count):
        if on_shot:
            on_shot(i + 1)
        before = {p.name for p in outdir.iterdir() if p.is_file()}
        try:
            # context-managed so the socket is always released, even on a slow/failed
            # transfer — otherwise fds leak across an event day of captures.
            with urllib.request.urlopen(
                    base + "/capture",
                    timeout=settings.camera.capture_timeout_seconds + 10) as r:
                data = json.loads(r.read().decode())
        except Exception as e:
            raise CaptureError(f"daemon capture request failed: {e}")
        if not data.get("ok"):
            raise CaptureError("camera capture failed (autofocus or transfer)")
        new = sorted(set(p.name for p in outdir.iterdir() if p.is_file()) - before)
        if not new:
            raise CaptureError("no image downloaded from camera")
        for name in new:
            src = outdir / name
            dst = dest_dir / f"shot_{i+1:02d}_{_ts()}{src.suffix}"
            shutil.move(str(src), str(dst))
            out.append(dst)
    return out


def make_camera(settings: Settings) -> CameraBackend:
    b = settings.camera.backend
    if b == "sony":
        return SonyCamera(settings)
    if b == "webcam":
        return WebcamCamera(settings.preview.webcam_index)
    return MockCamera(settings.general.booth_name)
