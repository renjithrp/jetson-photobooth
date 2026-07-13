#!/usr/bin/env python3
"""Standalone hand-gesture trigger worker (isolated MediaPipe).

WHY THIS EXISTS AS A SEPARATE PROCESS
-------------------------------------
MediaPipe (0.10.x) requires ``numpy<2``, but the booth's GPU AI stack requires
``numpy>=2`` (rembg needs >=2.3, opencv-python 5.0 needs >=2). Those constraints
cannot be satisfied in one virtualenv, so MediaPipe lives in its own venv
(``/opt/photobooth/gesture-venv``) and gesture detection runs here, out of process.

HOW IT HOOKS IN
---------------
It does NOT read the Sony camera daemon (:8080) directly — the CrSDK live-view
server serves a single client and ``sony_hub`` already owns it. Instead it reads
the backend's buffered, multi-client MJPEG at ``:8000/api/preview/stream`` (same
stream the kiosk/browser use), runs MediaPipe Hands, and when the configured
gesture is held long enough it POSTs to ``:8000/api/capture`` — the same session
start the in-process gesture path used. Detection logic mirrors
``backend/sony_hub.py::SonyFrameHub._detect``.

The in-hub gesture engine auto-disables when MediaPipe is absent from the main
venv (which it always will be), so this worker is the sole gesture path — no clash.
"""
from __future__ import annotations

import importlib.util
import json
import os
import ssl
import time
import urllib.request

APP = os.environ.get("BOOTH_APP", "/opt/photobooth")
DATA = os.environ.get("BOOTH_DATA", os.path.join(APP, "data"))
SETTINGS_PATH = os.path.join(DATA, "settings.json")
BACKEND = os.environ.get("BOOTH_BACKEND", "").rstrip("/")  # blank => auto-detect scheme

# Load the repo's gesture math WITHOUT importing the whole `backend` package
# (which pulls fastapi/pydantic that aren't in this venv). gestures.py is pure stdlib.
_spec = importlib.util.spec_from_file_location(
    "booth_gestures", os.path.join(APP, "backend", "gestures.py"))
gestures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gestures)

import cv2  # noqa: E402  (provided by the gesture-venv)
import mediapipe as mp  # noqa: E402
import numpy as np  # noqa: E402
from types import SimpleNamespace  # noqa: E402

_SSL_UNVERIFIED = ssl.create_default_context()
_SSL_UNVERIFIED.check_hostname = False
_SSL_UNVERIFIED.verify_mode = ssl.CERT_NONE


def _log(msg: str) -> None:
    print(f"[gesture] {msg}", flush=True)


def _opener(url: str, method: str = "GET", timeout: float = 10):
    req = urllib.request.Request(url, method=method)
    ctx = _SSL_UNVERIFIED if url.startswith("https") else None
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def _detect_backend() -> str:
    """Return the working backend base URL (handles HTTPS self-signed on :8000)."""
    if BACKEND:
        return BACKEND
    for base in ("http://127.0.0.1:8000", "https://127.0.0.1:8000"):
        try:
            _opener(base + "/api/preview/stream", timeout=3).close()
            return base
        except Exception:
            continue
    return "http://127.0.0.1:8000"  # default; the reader loop will keep retrying


def _load_trigger() -> SimpleNamespace | None:
    """Read the trigger settings block as an attribute holder (what gestures.py wants)."""
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        return SimpleNamespace(**data.get("trigger", {}))
    except Exception:
        return None


class GestureWorker:
    def __init__(self) -> None:
        self.base = _detect_backend()
        self.trigger = _load_trigger() or SimpleNamespace()
        self._settings_read = 0.0
        # 0.6/0.5 balances the two failure modes seen at the booth: 0.8/0.6 missed
        # small far-away hands in the low-res (1024x680) live view, while 0.5/0.5
        # fired on half-visible palms. The hold time + strict gesture-match +
        # cooldown filter the remaining weak detections.
        self._hands = mp.solutions.hands.Hands(
            max_num_hands=1, min_detection_confidence=0.6, min_tracking_confidence=0.5)
        self._face = None  # lazily created when require_face is on
        self._hold_start: float | None = None
        self._last_detected = 0.0
        self._last_fire = 0.0
        self._last_dbg = 0.0
        _log(f"backend={self.base} settings={SETTINGS_PATH}")
        _log(f"gesture={getattr(self.trigger, 'gesture_type', 'open_palm')} "
             f"mode={getattr(self.trigger, 'mode', 'gesture')}")

    # ---- settings (hot-reloaded so admin changes apply without a restart) -----
    def _refresh_settings(self) -> None:
        now = time.time()
        if now - self._settings_read < 3.0:
            return
        self._settings_read = now
        t = _load_trigger()
        if t is not None:
            self.trigger = t

    @property
    def _gesture_active(self) -> bool:
        return getattr(self.trigger, "mode", "gesture") in ("gesture", "both")

    def _face_detector(self):
        if self._face is None:
            self._face = mp.solutions.face_detection.FaceDetection(
                model_selection=0, min_detection_confidence=0.5)
        return self._face

    # ---- MJPEG reader -> detection -------------------------------------------
    def run(self) -> None:
        url = self.base + "/api/preview/stream"
        detect_period = 0.15  # ~6 fps hand detection
        last_detect = 0.0
        while True:
            try:
                resp = _opener(url, timeout=15)
            except Exception as e:
                _log(f"cannot reach preview {url}: {e}")
                time.sleep(2)
                continue
            _log("connected to preview stream")
            buf = b""
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    frame, buf = self._extract_jpeg(buf)
                    if frame and self._gesture_active and \
                            time.time() - last_detect > detect_period:
                        last_detect = time.time()
                        self._refresh_settings()
                        self._detect(frame)
                    if len(buf) > 4_000_000:
                        buf = buf[-1_000_000:]
            except Exception as e:
                _log(f"stream error: {e}")
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            time.sleep(1)

    @staticmethod
    def _extract_jpeg(buf: bytes):
        s = buf.find(b"\xff\xd8")
        if s < 0:
            return None, buf[-2:]
        e = buf.find(b"\xff\xd9", s + 2)
        if e < 0:
            return None, buf[s:]
        return buf[s:e + 2], buf[e + 2:]

    # ---- detection (mirrors sony_hub._detect) --------------------------------
    def _detect(self, jpeg: bytes) -> None:
        try:
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            t = self.trigger
            res = self._hands.process(rgb)
            hands_lm = res.multi_hand_landmarks
            score = 0.0
            if hands_lm and res.multi_handedness:
                try:
                    score = res.multi_handedness[0].classification[0].score
                except Exception:
                    score = 0.0
            gtype = getattr(t, "gesture_type", "open_palm")
            # NOTE: `score` is the left/right HANDEDNESS confidence (>=0.5 by
            # construction whenever a hand is found) — logged for debugging but
            # not gated on; detection confidence is enforced by the Hands()
            # min_detection_confidence instead.
            in_frame = bool(hands_lm) and \
                gestures.hand_fully_in_frame(hands_lm[0].landmark)
            gesture_ok = in_frame and \
                gestures.gesture_matches(gtype, hands_lm[0].landmark)
            detected = gesture_ok

            face_ok = None
            on_face = False
            if detected and getattr(t, "require_face", False):
                fres = self._face_detector().process(rgb)
                lm = hands_lm[0].landmark
                on_face = gestures.hand_on_face(lm[9].x, lm[9].y, fres.detections)
                face_ok = gestures.any_face_in_zone(fres.detections, t)
                if on_face:
                    detected = False

            now = time.time()
            if hands_lm and now - self._last_dbg > 1.5:
                self._last_dbg = now
                extra = (" | " + gestures.thumb_debug(hands_lm[0].landmark)
                         ) if gtype == "thumbs_up" else ""
                _log(f"hand: want={gtype} match={gesture_ok} score={score:.2f} "
                     f"in_frame={in_frame} on_face={on_face} in_zone={face_ok} "
                     f"-> fire={detected}{extra}")

            hold = float(getattr(t, "gesture_hold_seconds", 1.5))
            cooldown = float(getattr(t, "cooldown_seconds", 5.0))
            if detected:
                if now - self._last_fire < cooldown:
                    return
                self._last_detected = now
                self._hold_start = self._hold_start or now
                if now - self._hold_start >= hold:
                    self._hold_start = None
                    self._last_fire = now
                    delay = float(getattr(t, "gesture_start_delay", 0.0))
                    if delay > 0:
                        time.sleep(delay)
                    self._fire()
            elif self._hold_start and now - self._last_detected > 0.5:
                self._hold_start = None
        except Exception as e:
            _log(f"detect error: {e}")

    def _fire(self) -> None:
        try:
            r = _opener(self.base + "/api/capture", method="POST", timeout=8)
            body = r.read().decode()
            _log(f"gesture -> POST /api/capture : {body}")
        except Exception as e:
            _log(f"capture request failed: {e}")


def main() -> None:
    GestureWorker().run()


if __name__ == "__main__":
    main()
