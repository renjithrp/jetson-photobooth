"""Live preview sources, served to the browser as an MJPEG stream.

- MockLiveView   : animated Pillow frames (no hardware needed).
- WebcamLiveView : OpenCV VideoCapture (optional dependency).
- SonyHTTPLiveView: pulls JPEG frames from the CrSDK live-view HTTP sample.
"""
from __future__ import annotations

import io
import math
import time
from datetime import datetime

from PIL import Image, ImageDraw

from .models import Settings


class LiveViewSource:
    def read_jpeg(self) -> bytes | None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class MockLiveView(LiveViewSource):
    def __init__(self, mirror: bool = True) -> None:
        self.mirror = mirror
        self.t0 = time.time()

    def read_jpeg(self) -> bytes | None:
        w, h = 960, 640
        img = Image.new("RGB", (w, h), (24, 26, 32))
        d = ImageDraw.Draw(img)
        t = time.time() - self.t0
        cx = int(w / 2 + math.sin(t * 2) * 220)
        cy = int(h / 2 + math.cos(t * 1.5) * 120)
        d.ellipse((cx - 70, cy - 70, cx + 70, cy + 70), fill=(120, 200, 255))
        d.text((20, 20), "LIVE PREVIEW (mock)", fill=(240, 240, 240))
        d.text((20, h - 30), datetime.now().strftime("%H:%M:%S"), fill=(200, 200, 200))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=75)
        return buf.getvalue()


class WebcamLiveView(LiveViewSource):
    def __init__(self, index: int = 0, mirror: bool = True) -> None:
        import cv2
        self.cv2 = cv2
        self.mirror = mirror
        self.cap = cv2.VideoCapture(index)

    def read_jpeg(self) -> bytes | None:
        ok, frame = self.cap.read()
        if not ok:
            return None
        if self.mirror:
            frame = self.cv2.flip(frame, 1)
        ok, buf = self.cv2.imencode(".jpg", frame, [self.cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    def close(self) -> None:
        try:
            self.cap.release()
        except Exception:
            pass


class SonyHTTPLiveView(LiveViewSource):
    def __init__(self, url: str) -> None:
        self.url = url

    def read_jpeg(self) -> bytes | None:
        import urllib.request
        try:
            with urllib.request.urlopen(self.url, timeout=2) as r:
                return r.read()
        except Exception:
            return None


def make_source(settings: Settings) -> LiveViewSource:
    p = settings.preview
    if p.source == "webcam":
        try:
            return WebcamLiveView(p.webcam_index, p.mirror)
        except Exception as e:
            print(f"[liveview] webcam unavailable ({e}); using mock")
            return MockLiveView(p.mirror)
    if p.source == "sony_http":
        return SonyHTTPLiveView(p.sony_http_url)
    return MockLiveView(p.mirror)


async def stream(request, produce, fps: int = 15, closer=None):
    """MJPEG generator that stops when the client disconnects (no leaked threads/sockets).

    `produce()` returns one JPEG (called in a threadpool so blocking work — Pillow,
    OpenCV, the hub buffer read — never blocks the event loop)."""
    import asyncio
    boundary = b"--frame"
    delay = 1.0 / max(1, fps)
    loop = asyncio.get_event_loop()
    try:
        while True:
            if await request.is_disconnected():
                break
            frame = await loop.run_in_executor(None, produce)
            if frame:
                yield (boundary + b"\r\nContent-Type: image/jpeg\r\n"
                       + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                       + frame + b"\r\n")
            await asyncio.sleep(delay)
    finally:
        if closer:
            try:
                closer()
            except Exception:
                pass


def buffer_mjpeg(get_frame, fps: int = 15):
    """Serve MJPEG from a callable returning the latest JPEG bytes (the Sony hub).
    Same-origin via the backend; no upstream connection per client."""
    boundary = b"--frame"
    delay = 1.0 / max(1, fps)
    while True:
        frame = get_frame()
        if frame:
            yield (boundary + b"\r\nContent-Type: image/jpeg\r\n"
                   + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                   + frame + b"\r\n")
        time.sleep(delay)


def proxy_mjpeg(url: str):
    """Transparently pipe an upstream MJPEG stream (the Sony CrSDK live-view server)
    straight through to the browser, lowest latency, no re-encode."""
    import urllib.request
    resp = urllib.request.urlopen(url, timeout=10)
    try:
        while True:
            chunk = resp.read(16384)
            if not chunk:
                break
            yield chunk
    finally:
        resp.close()


def mjpeg_stream(settings: Settings):
    """Generator yielding multipart/x-mixed-replace JPEG frames."""
    source = make_source(settings)
    boundary = b"--frame"
    delay = 1.0 / max(1, settings.preview.fps)
    try:
        while True:
            frame = source.read_jpeg()
            if frame:
                yield (boundary + b"\r\nContent-Type: image/jpeg\r\n"
                       + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                       + frame + b"\r\n")
            time.sleep(delay)
    finally:
        source.close()
