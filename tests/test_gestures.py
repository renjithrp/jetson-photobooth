"""Unit tests for hand-gesture classification (no camera / MediaPipe needed)."""
from backend.gestures import gesture_matches


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
