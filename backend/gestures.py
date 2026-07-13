"""Shared hand-gesture classification from MediaPipe hand landmarks.

Landmark tip/pip indices: thumb 4/3, index 8/6, middle 12/10, ring 16/14, pinky 20/18.
A finger is "extended" when its tip is above its PIP joint (smaller y).
"""
from __future__ import annotations


def _dist(a, b) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


# tip/PIP landmark pairs for index, middle, ring, pinky
_FINGERS = ((8, 6), (12, 10), (16, 14), (20, 18))


def thumb_debug(lm) -> str:
    """Human-readable thumbs-up geometry for tuning (logged by the hub)."""
    hand = _dist(lm[0], lm[9]) or 1e-6

    def ext(tip, pip):
        return lm[tip].y < lm[pip].y
    count = sum(ext(t, p) for t, p in _FINGERS)
    above = round((lm[5].y - lm[4].y) / hand, 2)   # thumb above knuckle, in palm units
    return f"fingers_up={count}(need 0) thumb_above_knuckle={above}(need>0.10)"


def gesture_matches(gtype: str, lm) -> bool:
    hand = _dist(lm[0], lm[9]) or 1e-6

    def ext(tip, pip, margin=0.0):
        return lm[tip].y < lm[pip].y - margin * hand

    idx, mid, ring, pinky = (ext(t, p) for t, p in _FINGERS)
    count = sum([idx, mid, ring, pinky])
    thumb_up = lm[4].y < lm[3].y
    # thumb "out to the side": tip far from the index knuckle, relative to hand size
    thumb_out = (_dist(lm[4], lm[5]) / hand) > 0.7

    if gtype == "open_palm":
        # Tolerate one finger not registering (pinky/ring landmarks are noisy at
        # booth distance in the low-res live view), BUT the fingers that do count
        # must be CLEARLY extended — tip well above the PIP joint, scaled to hand
        # size — so a half-open / half-curled palm no longer fires.
        m = 0.15
        clear = sum(ext(t, p, m) for t, p in _FINGERS)
        return clear >= 3
    if gtype == "fist":
        return count == 0 and not thumb_up
    if gtype == "peace":          # V sign ✌
        return idx and mid and not ring and not pinky
    if gtype == "thumbs_up":      # 👍 thumb up above a closed fist
        if count != 0:
            return False
        m = 0.10 * hand           # thumb tip clearly above the knuckle line (scale-invariant)
        return lm[4].y < lm[5].y - m and lm[4].y < lm[3].y
    if gtype == "three":
        return idx and mid and ring and not pinky
    if gtype == "rock":           # index + pinky up 🤘
        return idx and not mid and not ring and pinky
    if gtype == "one":            # point up ☝️
        return idx and not mid and not ring and not pinky
    if gtype == "pinky":          # pinky only (pinky promise)
        return pinky and not idx and not mid and not ring
    if gtype == "call_me":        # 🤙 thumb + pinky
        return thumb_out and pinky and not idx and not mid and not ring
    if gtype == "love":           # 🤟 thumb + index + pinky
        return thumb_out and idx and pinky and not mid and not ring
    return count >= 1             # any_hand


def hand_fully_in_frame(lm, margin: float = 0.02) -> bool:
    """True when all five fingertips sit inside the frame. MediaPipe extrapolates
    landmarks for a hand that is partially outside the image, so a half-visible
    palm at the frame edge can otherwise read as an open palm. The wrist is
    deliberately NOT checked: in the normal booth pose the raised forearm enters
    from the bottom of the frame and the wrist may sit at/below the bottom edge."""
    return all(margin <= lm[i].x <= 1.0 - margin and
               margin <= lm[i].y <= 1.0 - margin
               for i in (4, 8, 12, 16, 20))


# ---------------------------------------------------------------------------
# Face-zone gating: require a detected face inside a target region before a
# gesture is allowed to fire. Region is a normalised box (x, y, w, h) in 0..1.
# ---------------------------------------------------------------------------

# Preset zones, normalised to the preview frame. Kept in sync with the admin UI.
FACE_REGION_PRESETS = {
    "full":          (0.00, 0.00, 1.00, 1.00),
    "center_square": (0.30, 0.12, 0.40, 0.76),
    "center_wide":   (0.12, 0.12, 0.76, 0.76),
}


def region_box(t) -> tuple[float, float, float, float]:
    """Resolve the active face zone (x, y, w, h) from a TriggerSettings."""
    preset = getattr(t, "face_region", "center_square")
    if preset == "custom":
        return (
            max(0.0, min(1.0, getattr(t, "face_region_x", 0.30))),
            max(0.0, min(1.0, getattr(t, "face_region_y", 0.12))),
            max(0.01, min(1.0, getattr(t, "face_region_w", 0.40))),
            max(0.01, min(1.0, getattr(t, "face_region_h", 0.76))),
        )
    return FACE_REGION_PRESETS.get(preset, FACE_REGION_PRESETS["center_square"])


def face_in_zone(rel_bbox, t) -> bool:
    """True when a face's bounding box centre sits inside the configured zone
    (and meets the optional minimum size). `rel_bbox` is a MediaPipe
    relative_bounding_box with .xmin/.ymin/.width/.height in 0..1."""
    x, y, w, h = region_box(t)
    cx = rel_bbox.xmin + rel_bbox.width / 2
    cy = rel_bbox.ymin + rel_bbox.height / 2
    if not (x <= cx <= x + w and y <= cy <= y + h):
        return False
    min_size = getattr(t, "face_min_size", 0.0) or 0.0
    if min_size > 0 and rel_bbox.width < min_size:
        return False
    return True


def any_face_in_zone(detections, t) -> bool:
    """True if any of the MediaPipe face detections falls in the zone."""
    for det in detections or []:
        try:
            if face_in_zone(det.location_data.relative_bounding_box, t):
                return True
        except Exception:
            continue
    return False


def hand_on_face(cx: float, cy: float, detections, shrink: float = 0.18) -> bool:
    """True when a hand's palm centre (cx, cy, normalised) sits inside a detected
    face box — i.e. MediaPipe Hands almost certainly mis-read the FACE as a hand.
    A real gesture is held away from the face, so this kills that false positive.
    The box is shrunk by `shrink` on each side so a gesture beside the cheek still
    counts."""
    for det in detections or []:
        try:
            b = det.location_data.relative_bounding_box
            mx, my = b.width * shrink, b.height * shrink
            if (b.xmin + mx) <= cx <= (b.xmin + b.width - mx) and \
               (b.ymin + my) <= cy <= (b.ymin + b.height - my):
                return True
        except Exception:
            continue
    return False
