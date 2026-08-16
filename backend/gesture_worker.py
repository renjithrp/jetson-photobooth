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


def _load_trigger(base: str = "") -> SimpleNamespace | None:
    """Read the trigger settings block as an attribute holder (what gestures.py wants).

    Prefer the backend API: this worker runs as `pb` but the backend runs as root
    and writes settings.json mode 600, so a direct file read fails with
    PermissionError — which silently pinned the worker to its default gesture,
    ignoring whatever was configured in admin. The API's /api/settings is
    unauthenticated on loopback and its `trigger` block carries no secrets. Fall
    back to the file only if the backend is unreachable."""
    if base:
        try:
            with _opener(base + "/api/settings", timeout=3) as r:
                data = json.loads(r.read().decode())
            return SimpleNamespace(**data.get("trigger", {}))
        except Exception:
            pass
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        return SimpleNamespace(**data.get("trigger", {}))
    except Exception:
        return None


class GestureWorker:
    # The accuracy gates (confirm_frames, match_ratio, hand_face_scale,
    # assoc_face_dist, max_hands) are SETTINGS (trigger block) so they can be
    # fine-tuned live from admin / the iPad while watching the overlay. Defaults
    # were measured at the booth — e.g. an open palm's landmark span reads
    # ~0.30-0.35 of the face bbox height, hallucinated hands ~0.10, so the
    # face-scale default 0.15 sits between them (face bboxes inflate up close, so real palms read ~0.2 of the bbox at arm-in-front range).
    ABS_MIN_FLOOR = 0.04   # the face-scaled size gate never drops below this

    def __init__(self) -> None:
        self.base = _detect_backend()
        self.trigger = _load_trigger(self.base) or SimpleNamespace()
        self._settings_read = 0.0
        # 0.6/0.5 balances the two failure modes seen at the booth: 0.8/0.6 missed
        # small far-away hands in the low-res (1024x680) live view, while 0.5/0.5
        # fired on half-visible palms. The hold time + strict gesture-match +
        # cooldown filter the remaining weak detections.
        self._max_hands = max(1, int(getattr(self.trigger, "max_hands", 1)))
        self._hands = self._make_hands(self._max_hands)
        self._face = None  # lazily created when require_face is on
        self._hold_start: float | None = None
        self._wave = gestures.WaveDetector()
        self._last_detected = 0.0
        self._last_fire = 0.0
        self._last_dbg = 0.0
        self._published_clear = False   # sent one "no hand" state since the hand left
        # confirmation state: jitter-proofing for the static-gesture hold
        self._streak = 0                # consecutive matching detection frames
        self._hold_hits = 0             # matched frames since the hold started
        self._hold_misses = 0           # unmatched frames since the hold started
        _log(f"backend={self.base} settings={SETTINGS_PATH}")
        _log(f"gesture={getattr(self.trigger, 'gesture_type', 'open_palm')} "
             f"mode={getattr(self.trigger, 'mode', 'gesture')}")

    # ---- settings (hot-reloaded so admin changes apply without a restart) -----
    def _refresh_settings(self) -> None:
        now = time.time()
        if now - self._settings_read < 3.0:
            return
        self._settings_read = now
        t = _load_trigger(self.base)
        if t is not None:
            self.trigger = t
            # max_hands is baked into the Hands graph — rebuild it on change
            mh = max(1, int(getattr(t, "max_hands", 1)))
            if mh != self._max_hands:
                self._max_hands = mh
                self._hands = self._make_hands(mh)
                _log(f"max_hands -> {mh}")

    @staticmethod
    def _make_hands(max_hands: int):
        return mp.solutions.hands.Hands(
            max_num_hands=max_hands,
            min_detection_confidence=0.6, min_tracking_confidence=0.5)

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
        detect_period = 0.15   # ~6 fps hand detection (plenty for a held/static gesture)
        wave_period = 0.05     # wave is a temporal gesture — sample as fast as the stream
        last_detect = 0.0      # allows so each side-to-side swing is actually caught
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
                    period = (wave_period
                              if getattr(self.trigger, "gesture_type", "") == "wave"
                              else detect_period)
                    if frame and self._gesture_active and \
                            time.time() - last_detect > period:
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

            # ---- per-frame face pass: the subject's face size calibrates the hand
            # size gate, anchors the hand to a person, and feeds the on-face and
            # in-zone checks. Runs only while a hand is present (cheap at ~6fps).
            faces = []
            if hands_lm:
                try:
                    faces = self._face_detector().process(rgb).detections or []
                except Exception:
                    faces = []
            # A face bbox can exceed the frame (partially-out-of-frame face reads
            # height > 1.0) — that's not a usable subject-size reference, so treat
            # oversized boxes as "no calibration" and fall back to hand_min_size.
            face_h = max((f.location_data.relative_bounding_box.height
                          for f in faces), default=0.0)
            if face_h > 0.9:
                face_h = 0.0

            # Evaluate EVERY tracked hand (trigger.max_hands) and act on the best
            # candidate: a fully passing hand wins; otherwise the largest hand is
            # what the overlay shows, so you can see why it was rejected.
            best = None
            for hl in (hands_lm or []):
                ev = self._eval_hand(hl.landmark, t, gtype, faces, face_h)
                key = (ev["detected"], ev["span"])
                if best is None or key > best[0]:
                    best = (key, ev, hl.landmark)
            if best is None:
                lm, ev = None, {"in_frame": False, "span": 0.0, "eff_min": 0.0,
                                "size_ok": True, "near_face": True, "on_face": False,
                                "gesture_ok": False, "detected": False}
            else:
                _, ev, lm = best
            detected = ev["detected"]

            face_ok = None
            if detected and getattr(t, "require_face", False):
                face_ok = gestures.any_face_in_zone(faces, t)

            now = time.time()
            if lm is not None and now - self._last_dbg > 1.5:
                self._last_dbg = now
                extra = (" | " + gestures.thumb_debug(lm)) if gtype == "thumbs_up" else ""
                if gtype == "wave":
                    extra += f" swings={self._wave.swings}"
                _log(f"hand: want={gtype} hands={len(hands_lm or [])} "
                     f"face_h={face_h:.2f} match={ev['gesture_ok']} score={score:.2f} "
                     f"in_frame={ev['in_frame']} span={ev['span']:.2f} "
                     f"min={ev['eff_min']:.2f} size_ok={ev['size_ok']} "
                     f"near_face={ev['near_face']} on_face={ev['on_face']} "
                     f"in_zone={face_ok} streak={self._streak} -> fire={detected}{extra}")

            hold = float(getattr(t, "gesture_hold_seconds", 1.5))
            cooldown = float(getattr(t, "cooldown_seconds", 5.0))
            self._step_trigger(detected, gtype, lm, now, t, hold, cooldown)
            self._publish(gtype, lm, ev["gesture_ok"], ev["in_frame"], ev["on_face"],
                          face_ok, now, hold, cooldown, ev["span"], ev["size_ok"],
                          ev["eff_min"], ev["near_face"])
        except Exception as e:
            _log(f"detect error: {e}")

    def _eval_hand(self, lm, t, gtype, faces, face_h) -> dict:
        """Run every per-hand gate on one hand's landmarks -> verdict dict.

        Size gate: MediaPipe hallucinates tiny "hands" on background patterns. A
        hand is proportional to its owner's face, so when a face is visible the
        minimum scales with the SUBJECT (near guest -> bigger hand required, far
        guest -> relaxed); falls back to the absolute hand_min_size when the face
        detector flickers out. Association: the hand must be near a face — a hand
        (real or hallucinated) floating far from any person can't fire."""
        in_frame = gestures.hand_fully_in_frame(lm)
        span = gestures.hand_span(lm)
        abs_min = float(getattr(t, "hand_min_size", 0.0))
        scale = float(getattr(t, "hand_face_scale", 0.15))
        eff_min = max(self.ABS_MIN_FLOOR, scale * face_h) \
            if (face_h > 0 and scale > 0) else abs_min
        size_ok = span >= eff_min
        near_face = True
        on_face = False
        if faces:
            cx, cy = lm[9].x, lm[9].y
            d, fh = min(
                ((((cx - (bb.xmin + bb.width / 2)) ** 2 +
                   (cy - (bb.ymin + bb.height / 2)) ** 2) ** 0.5), bb.height)
                for f in faces
                for bb in (f.location_data.relative_bounding_box,))
            assoc = float(getattr(t, "assoc_face_dist", 3.0))
            if assoc > 0:
                near_face = d <= assoc * max(fh, 1e-6)
            on_face = gestures.hand_on_face(cx, cy, faces)
        gesture_ok = in_frame and gestures.gesture_matches(gtype, lm)
        return {"in_frame": in_frame, "span": span, "eff_min": eff_min,
                "size_ok": size_ok, "near_face": near_face, "on_face": on_face,
                "gesture_ok": gesture_ok,
                "detected": gesture_ok and size_ok and near_face and not on_face}

    def _step_trigger(self, detected, gtype, lm, now, t, hold, cooldown) -> None:
        """Hold/cooldown/fire state machine (split from _detect so the overlay
        state can be published on every path, including the early returns here)."""
        try:
            if gtype == "wave":
                # Temporal trigger: no hold — fire the moment the palm finishes
                # its 3rd alternating swing (~ waving twice). Brief tracking
                # gaps (<0.5s) keep the swing count; longer ones reset it.
                if detected:
                    if now - self._last_fire < cooldown:
                        return
                    self._last_detected = now
                    if self._wave.update(now, lm):
                        self._last_fire = now
                        delay = float(getattr(t, "gesture_start_delay", 0.0))
                        if delay > 0:
                            time.sleep(delay)
                        self._fire()
                elif now - self._last_detected > 0.5:
                    self._wave.reset()
                return
            # Static gestures: jitter-proofed hold. A hold only STARTS after
            # CONFIRM_FRAMES consecutive matches (hallucinated matches are
            # unstable frame to frame; a real pose is steady), and only FIRES if
            # >= MATCH_RATIO of the frames across the hold window matched.
            confirm = int(getattr(t, "confirm_frames", 3))
            match_ratio = float(getattr(t, "match_ratio", 0.7))
            if detected:
                if now - self._last_fire < cooldown:
                    return
                self._last_detected = now
                self._streak += 1
                if not self._hold_start:
                    if self._streak < confirm:
                        return                      # still confirming
                    self._hold_start = now
                    self._hold_hits, self._hold_misses = 1, 0
                else:
                    self._hold_hits += 1
                ratio = self._hold_hits / (self._hold_hits + self._hold_misses)
                if now - self._hold_start >= hold and ratio >= match_ratio:
                    self._hold_start = None
                    self._streak = 0
                    self._last_fire = now
                    delay = float(getattr(t, "gesture_start_delay", 0.0))
                    if delay > 0:
                        time.sleep(delay)
                    self._fire()
            else:
                self._streak = 0
                if self._hold_start:
                    self._hold_misses += 1
                    total = self._hold_hits + self._hold_misses
                    ratio = self._hold_hits / total
                    # brief flicker (<0.5s) is tolerated, but a hold that's mostly
                    # misses is jitter, not a held pose — drop it early
                    if now - self._last_detected > 0.5 or \
                            (total >= 6 and ratio < match_ratio):
                        self._hold_start = None
                        self._hold_hits = self._hold_misses = 0
        except Exception as e:
            _log(f"detect error: {e}")

    def _publish(self, gtype, lm, gesture_ok, in_frame, on_face, face_ok,
                 now, hold, cooldown, span=0.0, size_ok=True, min_size=0.0,
                 near_face=True) -> None:
        """Push the detection verdict to the backend, which rebroadcasts it on the
        kiosk WebSocket bus — that's what the on-video gesture overlay draws. Sent
        per detection frame while a hand is visible, plus ONE clearing message when
        the hand leaves (so the overlay fades instead of freezing)."""
        if lm is None:
            if self._published_clear:
                return
            self._published_clear = True
            state = {"hand": False, "want": gtype}
        else:
            self._published_clear = False
            cd_left = max(0.0, cooldown - (now - self._last_fire)) if self._last_fire else 0.0
            state = {
                "hand": True,
                "want": gtype,
                "lm": [[round(p.x, 4), round(p.y, 4)] for p in lm],
                "match": bool(gesture_ok),
                "in_frame": bool(in_frame),
                "span": round(span, 3),
                "size_ok": bool(size_ok),
                "min_size": round(min_size, 3),
                "near_face": bool(near_face),
                "confirming": self._hold_start is None and self._streak > 0,
                "on_face": bool(on_face),
                "face_in_zone": face_ok,
                "hold_need": hold,
                "hold_progress": round(min(1.0, (now - self._hold_start) / hold), 3)
                                 if (self._hold_start and hold > 0) else 0.0,
                "cooldown_left": round(cd_left, 1),
                "swings": self._wave.swings if gtype == "wave" else None,
            }
        try:
            req = urllib.request.Request(
                self.base + "/api/gesture/state",
                data=json.dumps(state).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            ctx = _SSL_UNVERIFIED if self.base.startswith("https") else None
            urllib.request.urlopen(req, timeout=1.5, context=ctx).close()
        except Exception:
            pass  # overlay is best-effort; never let it stall detection

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
