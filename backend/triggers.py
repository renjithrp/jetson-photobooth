"""Trigger sources that start a capture session: GPIO button and hand gesture.

Each runs in its own daemon thread and calls `on_trigger()` (thread-safe). The
TriggerManager respects the configured mode and a cooldown after each session.
Optional deps (gpiod, opencv, mediapipe) are imported lazily; missing deps just
disable that trigger with a log line instead of crashing.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .models import Settings


class _BaseTrigger(threading.Thread):
    def __init__(self, on_trigger: Callable[[str], None]) -> None:
        super().__init__(daemon=True)
        self.on_trigger = on_trigger
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()


class GPIOTrigger(_BaseTrigger):
    def __init__(self, on_trigger, settings: Settings) -> None:
        super().__init__(on_trigger)
        self.s = settings.trigger

    def run(self) -> None:
        try:
            import gpiod
        except Exception as e:
            print(f"[trigger:gpio] gpiod not available ({e}); GPIO disabled")
            return
        try:
            chip = gpiod.Chip(self.s.gpio_chip)
            line = chip.get_line(self.s.gpio_line)
            line.request(consumer="photobooth",
                         type=gpiod.LINE_REQ_DIR_IN)
        except Exception as e:
            print(f"[trigger:gpio] could not open {self.s.gpio_chip}:{self.s.gpio_line} ({e})")
            return
        print(f"[trigger:gpio] watching {self.s.gpio_chip} line {self.s.gpio_line}")
        pressed_state = 0 if self.s.gpio_active_low else 1
        last = time.time()
        while not self._stop.is_set():
            try:
                val = line.get_value()
            except Exception:
                break
            if val == pressed_state and (time.time() - last) * 1000 > self.s.gpio_debounce_ms:
                last = time.time()
                self.on_trigger("gpio")
            time.sleep(0.02)


class GestureTrigger(_BaseTrigger):
    def __init__(self, on_trigger, settings: Settings) -> None:
        super().__init__(on_trigger)
        self.t = settings.trigger
        self.cam_index = settings.preview.webcam_index

    def run(self) -> None:
        try:
            import cv2
            import mediapipe as mp
        except Exception as e:
            print(f"[trigger:gesture] opencv/mediapipe not available ({e}); gesture disabled")
            return
        cap = cv2.VideoCapture(self.cam_index)
        if not cap.isOpened():
            print(f"[trigger:gesture] cannot open camera {self.cam_index} (in use by preview?)")
            return
        hands = mp.solutions.hands.Hands(max_num_hands=1, min_detection_confidence=0.6)
        face = None
        if self.t.require_face:
            face = mp.solutions.face_detection.FaceDetection(
                model_selection=0, min_detection_confidence=0.5)
            print(f"[trigger:gesture] face gating ON (zone={self.t.face_region})")
        print(f"[trigger:gesture] watching for '{self.t.gesture_type}'")
        hold_start = None
        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = hands.process(rgb)
            detected = False
            if res.multi_hand_landmarks:
                lm = res.multi_hand_landmarks[0].landmark
                detected = self._matches(lm)
            if detected and face is not None:
                from .gestures import any_face_in_zone
                if not any_face_in_zone(face.process(rgb).detections, self.t):
                    detected = False
            if detected:
                hold_start = hold_start or time.time()
                if time.time() - hold_start >= self.t.gesture_hold_seconds:
                    hold_start = None
                    self.on_trigger("gesture")
                    time.sleep(self.t.cooldown_seconds)
            else:
                hold_start = None
            time.sleep(0.03)
        cap.release()

    def _matches(self, lm) -> bool:
        from .gestures import gesture_matches
        return gesture_matches(self.t.gesture_type, lm)


class TriggerManager:
    def __init__(self, on_trigger: Callable[[str], None]) -> None:
        self.on_trigger = on_trigger
        self._threads: list[_BaseTrigger] = []

    def start(self, settings: Settings, skip_gesture: bool = False) -> None:
        """skip_gesture=True when the Sony frame-hub already runs gesture detection
        (so we don't also open a separate webcam for it)."""
        self.stop()
        mode = settings.trigger.mode
        if mode in ("gpio", "both"):
            self._threads.append(GPIOTrigger(self.on_trigger, settings))
        if mode in ("gesture", "both") and not skip_gesture:
            self._threads.append(GestureTrigger(self.on_trigger, settings))
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        for t in self._threads:
            t.stop()
        self._threads = []

    def restart(self, settings: Settings, skip_gesture: bool = False) -> None:
        self.start(settings, skip_gesture=skip_gesture)
