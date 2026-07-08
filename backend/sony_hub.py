"""Single-consumer hub for the Sony live-view MJPEG stream.

The CrSDK live-view server (`liveviewServer` on :8080) effectively serves ONE
client at a time. This hub is that one client: it reads the stream continuously,
keeps the latest JPEG frame for the kiosk preview, and runs MediaPipe hand-gesture
detection on the frames to trigger captures — all from a single camera session.

Preview is then served to browsers from the buffered frame (same-origin, no leak),
and gesture works without a separate camera.
"""
from __future__ import annotations

import threading
import time
import urllib.request
from typing import Callable, Optional

from .gestures import any_face_in_zone, gesture_matches, hand_on_face, thumb_debug
from .models import Settings


class SonyFrameHub:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._run = False
        self._gen = 0
        self._lock = threading.Lock()
        self.latest: Optional[bytes] = None
        self.url = "http://127.0.0.1:8080/"
        self.on_gesture: Optional[Callable[[str], None]] = None
        self.settings: Optional[Settings] = None
        self.connected = False
        # health/streaming metrics (for the watchdog + admin/kiosk status)
        self.last_frame = 0.0
        self.connected_since = 0.0
        self.fps = 0.0
        self._fps_count = 0
        self._fps_t0 = 0.0
        # gesture state
        self._gesture_enabled = False
        self._hands = None
        self._face = None
        self._cv2 = None
        self._np = None
        self._hold_start: Optional[float] = None
        self._last_detected = 0.0    # last frame the gesture was accepted (hold grace)
        self._last_fire = 0.0
        self._last_dbg = 0.0

    # ---- lifecycle --------------------------------------------------------
    def configure(self, settings: Settings, on_gesture: Callable[[str], None]) -> None:
        self.settings = settings
        self.url = settings.preview.sony_http_url or "http://127.0.0.1:8080/"
        self.on_gesture = on_gesture
        self._gesture_enabled = settings.trigger.mode in ("gesture", "both")

    def start(self) -> None:
        # bump generation so any previous reader loop exits and only this one runs
        self._run = True
        self._gen += 1
        gen = self._gen
        self._thread = threading.Thread(target=self._reader, args=(gen,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        self._gen += 1

    def restart(self, settings: Settings, on_gesture: Callable[[str], None]) -> None:
        self.configure(settings, on_gesture)
        self.start()  # bumps generation; old reader exits on its own

    def get_latest(self) -> Optional[bytes]:
        with self._lock:
            return self.latest

    # ---- health ----------------------------------------------------------
    def health(self) -> dict:
        now = time.time()
        age = (now - self.last_frame) if self.last_frame else None
        streaming = age is not None and age < 3.0
        return {
            "connected": self.connected,
            "streaming": streaming,
            "fps": round(self.fps, 1) if streaming else 0.0,
            "age_s": round(age, 1) if age is not None else None,
        }

    def stalled(self, threshold: float) -> bool:
        """True when the daemon is reachable (serving :8080) but no live-view frame
        has arrived for `threshold` seconds — the 'up but wedged' state systemd can't
        detect. Uses the connect time as the reference until the first frame arrives."""
        if not self.connected:
            return False
        ref = max(self.last_frame, self.connected_since)
        return ref > 0 and (time.time() - ref) > threshold

    # ---- gesture setup ----------------------------------------------------
    def _init_gesture(self) -> None:
        if not self._gesture_enabled:
            return
        try:
            import cv2
            import mediapipe as mp
            import numpy as np
            self._cv2 = cv2
            self._np = np
            self._hands = mp.solutions.hands.Hands(
                max_num_hands=1, min_detection_confidence=0.8, min_tracking_confidence=0.6)
            print(f"[hub] gesture detection ready ({self.settings.trigger.gesture_type})")
            if self.settings.trigger.require_face:
                self._face = mp.solutions.face_detection.FaceDetection(
                    model_selection=0, min_detection_confidence=0.5)
                print(f"[hub] face gating ON (zone={self.settings.trigger.face_region})")
        except Exception as e:
            print(f"[hub] gesture deps unavailable ({e}); gesture disabled")
            self._gesture_enabled = False

    # ---- main loop --------------------------------------------------------
    def _reader(self, gen: int) -> None:
        self._init_gesture()
        last_detect = 0.0
        detect_period = 0.15  # ~6 fps hand detection to keep CPU sane
        while self._run and gen == self._gen:
            try:
                resp = urllib.request.urlopen(self.url, timeout=10)
            except Exception as e:
                self.connected = False
                print(f"[hub] cannot reach live-view {self.url}: {e}")
                time.sleep(2)
                continue
            self.connected = True
            self.connected_since = time.time()
            buf = b""
            try:
                while self._run and gen == self._gen:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    frame, buf = self._extract_jpeg(buf)
                    if frame:
                        with self._lock:
                            self.latest = frame
                        # streaming metrics
                        tnow = time.time()
                        self.last_frame = tnow
                        self._fps_count += 1
                        if tnow - self._fps_t0 >= 1.0:
                            self.fps = self._fps_count / (tnow - self._fps_t0)
                            self._fps_count = 0
                            self._fps_t0 = tnow
                        if self._gesture_enabled and time.time() - last_detect > detect_period:
                            last_detect = time.time()
                            self._detect(frame)
                    if len(buf) > 4_000_000:  # safety: drop runaway buffer
                        buf = buf[-1_000_000:]
            except Exception as e:
                print(f"[hub] stream error: {e}")
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            self.connected = False
            time.sleep(1)

    @staticmethod
    def _extract_jpeg(buf: bytes):
        """Pull one complete JPEG (FFD8..FFD9) out of the buffer."""
        s = buf.find(b"\xff\xd8")
        if s < 0:
            return None, buf[-2:]
        e = buf.find(b"\xff\xd9", s + 2)
        if e < 0:
            return None, buf[s:]
        return buf[s:e + 2], buf[e + 2:]

    # ---- gesture detection ------------------------------------------------
    def _detect(self, jpeg: bytes) -> None:
        try:
            arr = self._np.frombuffer(jpeg, dtype=self._np.uint8)
            img = self._cv2.imdecode(arr, self._cv2.IMREAD_COLOR)
            if img is None:
                return
            rgb = self._cv2.cvtColor(img, self._cv2.COLOR_BGR2RGB)
            t = self.settings.trigger
            res = self._hands.process(rgb)
            hands_lm = res.multi_hand_landmarks
            # handedness classification confidence (low score => likely a false hand)
            score = 0.0
            if hands_lm and res.multi_handedness:
                try:
                    score = res.multi_handedness[0].classification[0].score
                except Exception:
                    score = 0.0
            gesture_ok = bool(hands_lm) and score >= 0.7 and \
                self._matches(hands_lm[0].landmark)
            detected = gesture_ok
            # When face gating is on we reject the ONE robust false-trigger case: a face
            # mis-read as a hand (palm centre sitting on a detected face) — that's what was
            # actually "firing with no one present". We deliberately do NOT also require a
            # face to be present inside the zone: at booth distance the short-range face
            # detector flickers, which was dropping genuine gestures and resetting the hold.
            # (face_ok is computed for the debug line only.)
            face_ok = None
            on_face = False
            if detected and t.require_face and self._face is not None:
                fres = self._face.process(rgb)
                lm = hands_lm[0].landmark
                on_face = hand_on_face(lm[9].x, lm[9].y, fres.detections)
                face_ok = any_face_in_zone(fres.detections, t)
                if on_face:
                    detected = False
            now = time.time()
            if hands_lm and now - self._last_dbg > 1.5:
                self._last_dbg = now
                extra = (" | " + thumb_debug(hands_lm[0].landmark)) if t.gesture_type == "thumbs_up" else ""
                print(f"[hub-dbg] hand: want={t.gesture_type} match={gesture_ok} "
                      f"score={score:.2f} on_face={on_face} in_zone={face_ok} -> fire={detected}{extra}")
            if detected:
                if now - self._last_fire < t.cooldown_seconds:
                    return
                self._last_detected = now
                self._hold_start = self._hold_start or now
                if now - self._hold_start >= t.gesture_hold_seconds:
                    self._hold_start = None
                    self._last_fire = now
                    print("[hub] gesture detected -> trigger")
                    if t.gesture_start_delay > 0:
                        time.sleep(t.gesture_start_delay)
                    if self.on_gesture:
                        self.on_gesture("gesture")
            # Tolerate brief detection gaps (hand-tracking / face-detect flicker): only
            # reset the hold once the gesture has been absent for >0.5s, so a steadily
            # held pose isn't restarted by a dropped frame or two.
            elif self._hold_start and now - self._last_detected > 0.5:
                self._hold_start = None
        except Exception:
            pass

    def _matches(self, lm) -> bool:
        return gesture_matches(self.settings.trigger.gesture_type, lm)


hub = SonyFrameHub()
