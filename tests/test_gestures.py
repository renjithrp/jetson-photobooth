"""Unit tests for hand-gesture classification (no camera / MediaPipe needed)."""
from backend.gestures import WaveDetector, gesture_matches, hand_fully_in_frame


class LM:
    def __init__(self, x, y):
        self.x, self.y, self.z = x, y, 0.0


def hand(index=False, middle=False, ring=False, pinky=False,
         thumb_up=False, thumb_out=False):
    """Build 21 landmarks for an upright hand. Finger 'extended' => tip.y < pip.y;
    thumb_out => thumb tip far from the index knuckle (relative to hand size)."""
    lm = [LM(0.5, 0.5) for _ in range(21)]
    lm[0] = LM(0.5, 1.0)        # wrist
    lm[9] = LM(0.5, 0.5)        # middle MCP  -> hand size = 0.5
    lm[5] = LM(0.45, 0.55)      # index MCP
    up, down, pip = 0.3, 0.7, 0.5
    lm[6], lm[8] = LM(0.5, pip), LM(0.5, up if index else down)
    lm[10], lm[12] = LM(0.5, pip), LM(0.5, up if middle else down)
    lm[14], lm[16] = LM(0.5, pip), LM(0.5, up if ring else down)
    lm[18], lm[20] = LM(0.5, pip), LM(0.5, up if pinky else down)
    lm[3] = LM(0.45, 0.6)       # thumb IP
    if thumb_out:
        lm[4] = LM(0.05, 0.55)  # far from index MCP -> thumb out to the side
    elif thumb_up:
        lm[4] = LM(0.45, 0.3)   # tip above IP -> thumb up
    else:
        lm[4] = LM(0.45, 0.75)  # folded
    return lm


def test_open_palm():
    assert gesture_matches("open_palm", hand(True, True, True, True))
    assert not gesture_matches("fist", hand(True, True, True, True))


def test_open_palm_tolerates_one_noisy_finger():
    # pinky landmark noise at booth distance: the pinky reads only LOOSELY
    # extended (above its PIP, but not clearly) — still an open palm as long as
    # the other three are near-straight.
    lm = hand(True, True, True, False)
    lm[20] = LM(0.5, 0.45)          # pip at 0.5: up, but not by the 0.30 margin
    assert gesture_matches("open_palm", lm)


def test_open_palm_rejects_finger_count_poses():
    # Deliberate non-palm poses always have a finger fully DOWN — they must not
    # read as an open palm even when three fingers are dead straight.
    assert not gesture_matches("open_palm", hand(True, True, True, False))   # "three"
    assert not gesture_matches("open_palm", hand(True, True, False, False))  # "peace"
    assert not gesture_matches("open_palm", hand(True, False, False, True))  # "rock"


def test_open_palm_rejects_half_open_hand():
    # half-curled palm: every tip only a hair above its PIP joint — must NOT fire.
    # hand size = 0.5, so the 0.15*hand margin requires tip.y < pip - 0.075.
    lm = hand()
    for tip in (8, 12, 16, 20):
        lm[tip] = LM(0.5, 0.46)     # pip is at 0.5 -> barely above, not clearly
    assert not gesture_matches("open_palm", lm)


def test_open_palm_rejects_relaxed_hand():
    # A casually raised hand: fingers up but with their natural half-curl — tips
    # ~0.24 hand-units above the PIPs. Reads as "extended" by the loose test but
    # must NOT count as a deliberate open palm (needs 0.30 = near-straight).
    lm = hand()
    for tip in (8, 12, 16, 20):
        lm[tip] = LM(0.5, 0.38)     # pip at 0.5, hand size 0.5 -> 0.24 hand-units
    assert not gesture_matches("open_palm", lm)
    # ...but the same relaxed pose still counts as a wave frame (looser 0.15
    # margin, motion does the discriminating there)
    assert gesture_matches("wave", lm)


def test_hand_fully_in_frame():
    # wrist at the very bottom edge (arm raised from below the frame) is fine —
    # only the fingertips must be visible
    lm = hand(True, True, True, True)
    assert lm[0].y == 1.0
    assert hand_fully_in_frame(lm)
    lm[20] = LM(1.01, 0.3)          # pinky tip past the frame edge -> half-visible
    assert not hand_fully_in_frame(lm)


def test_fist():
    assert gesture_matches("fist", hand())
    assert not gesture_matches("open_palm", hand())


def test_peace_v_sign():
    assert gesture_matches("peace", hand(index=True, middle=True))


def test_thumbs_up():
    assert gesture_matches("thumbs_up", hand(thumb_up=True))
    assert not gesture_matches("fist", hand(thumb_up=True))


def test_thumbs_up_rejects_loose_pose():
    # fingers curled but the thumb is NOT clearly raised above the fist (a relaxed
    # resting hand) — must NOT count as thumbs-up (the real-world false trigger).
    lm = hand()                    # all fingers folded, thumb folded
    lm[4] = LM(0.45, 0.65)         # thumb tip only slightly up, level with the knuckles
    assert not gesture_matches("thumbs_up", lm)


def test_three():
    assert gesture_matches("three", hand(index=True, middle=True, ring=True))


def test_rock():
    assert gesture_matches("rock", hand(index=True, pinky=True))


def test_one():
    assert gesture_matches("one", hand(index=True))
    assert not gesture_matches("one", hand(index=True, middle=True))


def test_pinky():
    assert gesture_matches("pinky", hand(pinky=True))


def test_call_me():
    assert gesture_matches("call_me", hand(pinky=True, thumb_out=True))
    assert not gesture_matches("call_me", hand(pinky=True))          # needs thumb out


def test_love():
    assert gesture_matches("love", hand(index=True, pinky=True, thumb_out=True))
    assert not gesture_matches("love", hand(index=True, pinky=True))  # needs thumb out


def test_any_hand():
    assert gesture_matches("any_hand", hand(index=True))
    assert not gesture_matches("any_hand", hand())


# ---- wave (👋 temporal trigger) --------------------------------------------
# The test hand's size (wrist->middle MCP) is 0.5, so one qualifying swing must
# travel >= MIN_SWING * 0.5 = 0.45 in frame units.

def wave_hand(dx):
    """Open palm shifted horizontally by dx (simulates the hand mid-wave)."""
    lm = hand(True, True, True, True)
    for p in lm:
        p.x += dx
    return lm


def test_wave_pose_is_open_palm():
    assert gesture_matches("wave", hand(True, True, True, True))
    assert not gesture_matches("wave", hand())


def test_wave_fires_on_third_alternating_swing():
    w = WaveDetector()
    assert not w.update(0.00, wave_hand(0.0))     # anchor
    assert not w.update(0.15, wave_hand(+0.5))    # swing 1 (right)
    assert not w.update(0.30, wave_hand(-0.0))    # swing 2 (back left)
    assert w.update(0.45, wave_hand(+0.5))        # swing 3 -> fire


def test_wave_static_palm_never_fires():
    w = WaveDetector()
    assert not any(w.update(i * 0.15, wave_hand(0.0)) for i in range(40))


def test_wave_ignores_small_jitter():
    w = WaveDetector()
    assert not any(w.update(i * 0.15, wave_hand(0.1 * (-1) ** i))
                   for i in range(40))


def test_wave_one_direction_sweep_never_fires():
    # A hand steadily crossing the frame is not a wave.
    w = WaveDetector()
    assert not any(w.update(i * 0.15, wave_hand(i * 0.05)) for i in range(20))


def test_wave_count_resets_after_pause():
    w = WaveDetector()
    assert not w.update(0.00, wave_hand(0.0))
    assert not w.update(0.15, wave_hand(+0.5))    # swing 1
    assert not w.update(0.30, wave_hand(0.0))     # swing 2
    # hand pauses > MAX_IDLE: the count must restart, so the next two swings
    # only reach 2 again and nothing fires
    assert not w.update(2.00, wave_hand(0.0))     # reset (idle)
    assert not w.update(2.15, wave_hand(+0.5))    # swing 1 again
    assert not w.update(2.30, wave_hand(0.0))     # swing 2
    assert w.swings == 2
