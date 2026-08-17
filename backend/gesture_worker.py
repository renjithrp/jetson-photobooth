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
    # measured at the booth (stats panel, 40min of frames): a real palm's
    # landmark span reads 0.6-0.9 of the face bbox height; hallucinated
    # background "hands" read <=0.43 of it — hand_face_scale 0.45 splits them.
    ABS_MIN_FLOOR = 0.05   # the face-scaled size gate never drops below this
    PERSON_TTL = 1.5       # seconds a tracked person survives without their face
    ZONE_GRACE = 1.0       # zone eligibility persists this long through face flicker
    MAX_PEOPLE = 3         # per-person hand-search crops per tick (largest first)

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
        self._face = None      # short-range face model (lazy)
        self._face_far = None  # full-range fallback for distant subjects (lazy)
        self._hold_start: float | None = None
        self._wave = gestures.WaveDetector()
        self._last_detected = 0.0
        self._last_fire = 0.0
        self._last_dbg = 0.0
        self._published_clear = False   # sent one "no hand" state since the hand left
        self._dry_fire = 0.0            # last tune-mode "would fire" (capture suppressed)
        # person tracking + per-person confirmation state (no global palm state)
        self._people: dict = {}         # pid -> tracker + gesture state
        self._next_pid = 1
        self._rearmed = True            # palms-down rearm after each fire
        self._rearm_wait = False
        self._clear_since = None
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

    def _far_face_detector(self):
        # full-range model (~5m) for when the short-range one gives out
        if self._face_far is None:
            self._face_far = mp.solutions.face_detection.FaceDetection(
                model_selection=1, min_detection_confidence=0.4)
        return self._face_far

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

    # ---- person tracking (spec's ByteTrack role, booth-sized) ----------------
    # Faces are few and move slowly at a booth, so a greedy nearest-face matcher
    # gives persistent per-person ids without a tracking dependency.
    def _track_people(self, faces, face_fix, now) -> None:
        seen = []
        for f in faces:
            bb = f.location_data.relative_bounding_box
            fh = bb.height * face_fix
            if fh <= 0 or fh > 0.9:      # out-of-frame face: unusable reference
                continue
            seen.append((bb.xmin + bb.width / 2, bb.ymin + bb.height / 2, fh, bb))
        assigned = set()
        for cx, cy, fh, bb in sorted(seen, key=lambda s: -s[2]):
            best, bestd = None, 1e9
            for pid, p in self._people.items():
                if pid in assigned or pid == 0:   # 0 = faceless-fallback pseudo person
                    continue
                d = ((cx - p["cx"]) ** 2 + (cy - p["cy"]) ** 2) ** 0.5
                if d < 1.5 * max(fh, p["fh"]) and d < bestd:
                    best, bestd = pid, d
            if best is None:
                best = self._next_pid
                self._next_pid += 1
                self._people[best] = {"streak": 0, "hold": None, "hits": 0,
                                      "misses": 0, "last_match": 0.0, "zone_ok": 0.0}
                _log(f"person {best} appeared")
            self._people[best].update(cx=cx, cy=cy, fh=fh, bb=bb, last_seen=now)
            assigned.add(best)
        for pid in [pid for pid, p in self._people.items()
                    if now - p.get("last_seen", 0) > self.PERSON_TTL]:
            _log(f"person {pid} left")
            del self._people[pid]

    def _eligible(self, p, t, now) -> bool:
        """Trigger-zone gate: only people whose face sits inside the configured
        zone may trigger (with a grace window so face flicker doesn't drop them)."""
        if not getattr(t, "require_face", False):
            return True
        if gestures.face_in_zone(p["bb"], t):
            p["zone_ok"] = now
        return (now - p["zone_ok"]) < self.ZONE_GRACE

    def _person_crop(self, rgb, p):
        """Subject ROI: MediaPipe's palm detector downscales its input to ~192px,
        so a distant hand vanishes in the full frame. A crop around each person's
        face (hands are raised near the head) is an effective 2-4x zoom; the
        landmarks are remapped to full-frame coordinates afterwards."""
        fh, cx, cy = p["fh"], p["cx"], p["cy"]
        x0, x1 = max(0.0, cx - 3.0 * fh), min(1.0, cx + 3.0 * fh)
        y0, y1 = max(0.0, cy - 3.0 * fh), min(1.0, cy + 2.5 * fh)
        H, W = rgb.shape[:2]
        px0, px1 = int(x0 * W), int(x1 * W)
        py0, py1 = int(y0 * H), int(y1 * H)
        if px1 - px0 < 40 or py1 - py0 < 40:
            return None, None
        return (np.ascontiguousarray(rgb[py0:py1, px0:px1]),
                (x0, y0, x1 - x0, y1 - y0))

    def _hands_in(self, res, box):
        """(full-frame landmarks, handedness label, score) per detected hand."""
        out = []
        lms = res.multi_hand_landmarks or []
        hs = res.multi_handedness or []
        for i, hl in enumerate(lms):
            handed, score = None, 0.0
            if i < len(hs):
                try:
                    handed = hs[i].classification[0].label
                    score = hs[i].classification[0].score
                except Exception:
                    pass
            if box:
                zx, zy, zw, zh = box
                pts = [SimpleNamespace(x=zx + q.x * zw, y=zy + q.y * zh)
                       for q in hl.landmark]
            else:
                pts = list(hl.landmark)
            out.append((pts, handed, score))
        return out

    # ---- detection: faces -> people -> per-person ROI -> hands -> per-person
    #      confirmation -> single trigger (see the pipeline note in the header)
    def _detect(self, jpeg: bytes) -> None:
        try:
            arr = np.frombuffer(jpeg, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                return
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            t = self.trigger
            gtype = getattr(t, "gesture_type", "open_palm")
            now = time.time()

            # faces: short-range model first; full-range (~5m) fallback whose
            # boxes read about half the size, x2 keeps the size calibration.
            try:
                faces = self._face_detector().process(rgb).detections or []
            except Exception:
                faces = []
            face_fix = 1.0
            if not faces:
                try:
                    faces = self._far_face_detector().process(rgb).detections or []
                    face_fix = 2.0
                except Exception:
                    faces = []
            self._track_people(faces, face_fix, now)

            # one Hands pass per (eligible, currently visible) person's ROI,
            # largest faces first; full-frame fallback when no usable person
            best = None            # (key, ev, pts, pid)
            hands_n = 0
            zoomed = False
            crops = 0
            for pid, p in sorted(self._people.items(), key=lambda kv: -kv[1]["fh"]):
                if crops >= self.MAX_PEOPLE:
                    break
                if now - p.get("last_seen", 0) > 0.5 or not self._eligible(p, t, now):
                    continue
                crop, box = self._person_crop(rgb, p)
                if crop is None:
                    continue
                crops += 1
                zoomed = True
                cand_best = None
                for pts, handed, score in self._hands_in(self._hands.process(crop), box):
                    hands_n += 1
                    ev = self._eval_hand(pts, t, gtype, handed,
                                         fh=p["fh"], fcx=p["cx"], fcy=p["cy"])
                    ev["score"] = score
                    key = (ev["detected"], ev["span"])
                    if cand_best is None or key > cand_best[0]:
                        cand_best = (key, ev, pts)
                    if best is None or key > best[0]:
                        best = (key, ev, pts, pid)
                p["match_now"] = bool(cand_best and cand_best[1]["detected"])
            visible = sum(1 for q, p in self._people.items()
                          if q != 0 and now - p.get("last_seen", 0) <= 0.5)
            if crops == 0 and visible == 0:
                # NO face anywhere (not even the far model): full-frame pass with
                # the absolute gates, tracked as pseudo-person 0. Deliberately NOT
                # used when people are visible but zone-ineligible — that would
                # bypass the trigger zone.
                for pts, handed, score in self._hands_in(self._hands.process(rgb), None):
                    hands_n += 1
                    ev = self._eval_hand(pts, t, gtype, handed)
                    ev["score"] = score
                    key = (ev["detected"], ev["span"])
                    if best is None or key > best[0]:
                        best = (key, ev, pts, 0)

            if best is None:
                lm, ev, pid = None, {"in_frame": False, "span": 0.0, "eff_min": 0.0,
                                     "size_ok": True, "near_face": True,
                                     "on_face": False, "gesture_ok": False,
                                     "detected": False, "palm": None, "score": 0.0}, None
            else:
                _, ev, lm, pid = best

            hold = float(getattr(t, "gesture_hold_seconds", 1.5))
            cooldown = float(getattr(t, "cooldown_seconds", 5.0))
            confirm = int(getattr(t, "confirm_frames", 3))
            ratio = float(getattr(t, "match_ratio", 0.7))

            # ---- trigger state ----
            armed = self._armed(now, cooldown)
            if gtype == "wave":
                # wave stays a single global temporal detector on the best hand
                if armed:
                    self._step_wave(ev["detected"], lm, now, t, cooldown)
            else:
                # per-person confirmation (spec: no global palm state) — the full-
                # frame fallback uses pseudo-person 0 so a faceless frame still works
                if best is not None and pid == 0:
                    p0 = self._people.setdefault(0, {
                        "streak": 0, "hold": None, "hits": 0, "misses": 0,
                        "last_match": 0.0, "zone_ok": 0.0,
                        "cx": 0.5, "cy": 0.5, "fh": 0.0})
                    p0["last_seen"] = now
                    p0["match_now"] = ev["detected"]
                for qid, p in list(self._people.items()):
                    self._step_person(qid, p, bool(p.get("match_now")), t, now,
                                      hold, cooldown, confirm, ratio, armed)
                    p["match_now"] = False

            face_ok = None
            if ev["detected"] and getattr(t, "require_face", False):
                face_ok = gestures.any_face_in_zone(faces, t)

            if lm is not None and now - self._last_dbg > 1.5:
                self._last_dbg = now
                extra = (" | " + gestures.thumb_debug(lm)) if gtype == "thumbs_up" else ""
                if gtype == "wave":
                    extra += f" swings={self._wave.swings}"
                pstate = self._people.get(pid, {})
                _log(f"hand: want={gtype} people={len(self._people)} pid={pid} "
                     f"hands={hands_n} crops={crops} "
                     f"match={ev['gesture_ok']} score={ev['score']:.2f} "
                     f"in_frame={ev['in_frame']} span={ev['span']:.2f} "
                     f"min={ev['eff_min']:.2f} size_ok={ev['size_ok']} "
                     f"near_face={ev['near_face']} on_face={ev['on_face']} "
                     f"palm={ev.get('palm')} in_zone={face_ok} armed={armed} "
                     f"streak={pstate.get('streak', 0)} -> fire={ev['detected']}{extra}")

            self._publish(gtype, lm, ev["gesture_ok"], ev["in_frame"], ev["on_face"],
                          face_ok, now, hold, cooldown, ev["span"], ev["size_ok"],
                          ev["eff_min"], ev["near_face"],
                          face_h=self._people.get(pid, {}).get("fh", 0.0) if pid else 0.0,
                          hands=hands_n, score=ev["score"], palm=ev.get("palm"),
                          zoom=zoomed, person=self._people.get(pid),
                          people_n=len([q for q in self._people if q != 0]),
                          armed=armed, rearm_wait=self._rearm_wait)
        except Exception as e:
            _log(f"detect error: {e}")

    def _eval_hand(self, lm, t, gtype, handed=None, fh=0.0, fcx=None, fcy=None) -> dict:
        """Run every per-hand gate on one hand's landmarks -> verdict dict.

        Size gate: MediaPipe hallucinates tiny "hands" on background patterns. A
        hand is proportional to its owner's face, so the minimum scales with the
        SUBJECT this hand was found on (fh); falls back to the absolute
        hand_min_size for the faceless full-frame pass. Association: the hand
        must be near ITS person's face, and a hand sitting ON the face is a
        face mis-read."""
        in_frame = gestures.hand_fully_in_frame(lm)
        span = gestures.hand_span(lm)
        abs_min = float(getattr(t, "hand_min_size", 0.0))
        scale = float(getattr(t, "hand_face_scale", 0.45))
        eff_min = max(self.ABS_MIN_FLOOR, scale * fh) \
            if (fh > 0 and scale > 0) else abs_min
        size_ok = span >= eff_min
        near_face = True
        on_face = False
        if fcx is not None and fh > 0:
            cx, cy = lm[9].x, lm[9].y
            d = ((cx - fcx) ** 2 + (cy - fcy) ** 2) ** 0.5
            assoc = float(getattr(t, "assoc_face_dist", 4.0))
            if assoc > 0:
                near_face = d <= assoc * fh
            on_face = d < 0.45 * fh
        gesture_ok = in_frame and gestures.gesture_matches(gtype, lm, handed)
        return {"in_frame": in_frame, "span": span, "eff_min": eff_min,
                "size_ok": size_ok, "near_face": near_face, "on_face": on_face,
                "gesture_ok": gesture_ok,
                "palm": gestures.palm_side(lm, handed),
                "detected": gesture_ok and size_ok and near_face and not on_face}

    # ---- trigger state machines ----------------------------------------------
    def _armed(self, now, cooldown) -> bool:
        """One trigger per session, then palms-DOWN rearm: after a fire, every
        matching palm must disappear and STAY clear for rearm_clear_seconds
        before a new confirmation can begin — a hand held up through the whole
        photo can't immediately retrigger when the cooldown lapses."""
        self._rearm_wait = False
        if self._last_fire and now - self._last_fire < cooldown:
            return False
        if self._last_fire and not self._rearmed:
            self._rearm_wait = True
            if any(now - p.get("last_match", 0) < 0.4 for p in self._people.values()):
                self._clear_since = None
                return False
            if self._clear_since is None:
                self._clear_since = now
                return False
            if now - self._clear_since < float(getattr(self.trigger,
                                                       "rearm_clear_seconds", 0.7)):
                return False
            self._rearmed = True
            self._rearm_wait = False
            _log("gesture rearmed (palms clear)")
        return True

    def _step_wave(self, detected, lm, now, t, cooldown) -> None:
        """Wave: single global temporal detector on the best hand — fires on the
        3rd alternating swing; brief tracking gaps keep the count."""
        if detected and lm is not None:
            self._last_detected = now
            if self._wave.update(now, lm):
                self._last_fire = now
                delay = float(getattr(t, "gesture_start_delay", 0.0))
                if delay > 0:
                    time.sleep(delay)
                self._fire()
        elif now - self._last_detected > 0.5:
            self._wave.reset()

    def _step_person(self, pid, p, detected, t, now, hold, cooldown,
                     confirm, ratio, armed) -> None:
        """Per-person jitter-proofed hold (no global palm state): a hold STARTS
        after `confirm` consecutive matching frames and FIRES only if >= `ratio`
        of the frames across the hold window matched. Any one person confirming
        triggers the booth; _armed() then locks everyone out."""
        if detected:
            p["last_match"] = now
        if not armed:
            p["streak"], p["hold"] = 0, None
            return
        if detected:
            p["streak"] += 1
            if not p["hold"]:
                if p["streak"] < confirm:
                    return                      # still confirming
                p["hold"] = now
                p["hits"], p["misses"] = 1, 0
            else:
                p["hits"] += 1
            r = p["hits"] / (p["hits"] + p["misses"])
            if now - p["hold"] >= hold and r >= ratio:
                p["hold"], p["streak"] = None, 0
                self._last_fire = now
                self._rearmed = False
                self._clear_since = None
                _log(f"person {pid} confirmed ({p['hits']}/{p['hits'] + p['misses']}) -> trigger")
                delay = float(getattr(t, "gesture_start_delay", 0.0))
                if delay > 0:
                    time.sleep(delay)
                self._fire()
        else:
            p["streak"] = 0
            if p["hold"]:
                p["misses"] += 1
                total = p["hits"] + p["misses"]
                # brief flicker (<0.5s) is tolerated, but a hold that's mostly
                # misses is jitter, not a held pose — drop it early
                if now - p["last_match"] > 0.5 or \
                        (total >= 6 and p["hits"] / total < ratio):
                    p["hold"] = None
                    p["hits"] = p["misses"] = 0

    def _publish(self, gtype, lm, gesture_ok, in_frame, on_face, face_ok,
                 now, hold, cooldown, span=0.0, size_ok=True, min_size=0.0,
                 near_face=True, face_h=0.0, hands=0, score=0.0, palm=None,
                 zoom=False, person=None, people_n=0, armed=True,
                 rearm_wait=False) -> None:
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
                "palm": palm,
                "zoom": bool(zoom),
                "confirming": bool(person and not person.get("hold")
                                   and person.get("streak", 0) > 0),
                "on_face": bool(on_face),
                "face_in_zone": face_ok,
                "hold_need": hold,
                "hold_progress": round(min(1.0, (now - person["hold"]) / hold), 3)
                                 if (person and person.get("hold") and hold > 0) else 0.0,
                "cooldown_left": round(cd_left, 1),
                "swings": self._wave.swings if gtype == "wave" else None,
                # tuning telemetry (for the on-screen stats panel)
                "face_h": round(face_h, 3),
                "hands": hands,
                "score": round(score, 2),
                "people": people_n,
                "armed": bool(armed),
                "rearm_wait": bool(rearm_wait),
                "streak": person.get("streak", 0) if person else 0,
                "hold_ratio": round(person["hits"] /
                                    (person["hits"] + person["misses"]), 2)
                              if (person and person.get("hold")
                                  and (person["hits"] + person["misses"]))
                              else None,
                "tune_mode": bool(getattr(self.trigger, "tune_mode", False)),
                "would_fire": (now - self._dry_fire) < 2.5,
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
        # Tune mode: everything up to the shutter behaves identically (hold,
        # cooldown, overlay) but the capture request is suppressed, so the gates
        # can be tuned live without wasting shots. The overlay shows WOULD FIRE.
        if bool(getattr(self.trigger, "tune_mode", False)):
            self._dry_fire = time.time()
            _log("gesture matched -> WOULD FIRE (tune mode, capture suppressed)")
            return
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
