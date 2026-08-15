"""Consent + delivery-dedup engine: the guarantee that a photo is never sent twice."""
from PIL import Image

from backend import config
from backend.consent import ConsentStore, normalize_phone


def make_photo(session: str, name: str) -> str:
    """Create a real capture file and return its guest-facing /captures URL."""
    d = config.captures_dir() / session
    d.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (200, 100, 50)).save(d / name, "JPEG")
    return f"/captures/{session}/{name}"


def fresh() -> ConsentStore:
    s = ConsentStore()
    s._load()
    s.whatsapp, s.drive_wanted, s.drive_uploaded = {}, [], []
    s._loaded = True
    return s


# ---- phone normalization --------------------------------------------------
def test_normalize_phone():
    assert normalize_phone("+1 (555) 123-4567") == "15551234567"
    assert normalize_phone("0044 7911 123456") == "447911123456"   # 00 == +
    assert normalize_phone("12345") is None                        # too short
    assert normalize_phone("") is None


# ---- WhatsApp opt-in + dedup ----------------------------------------------
def test_whatsapp_optin_dedups_repeat():
    s = fresh()
    a, b = make_photo("s1", "a.jpg"), make_photo("s1", "b.jpg")
    r1 = s.whatsapp_optin("+1 555 111 2222", [a, b])
    assert r1["ok"] and r1["added"] == 2
    # opting in AGAIN (same number, overlapping photos) adds nothing new
    r2 = s.whatsapp_optin("1-555-111-2222", [a, b])
    assert r2["ok"] and r2["added"] == 0 and r2["total"] == 2


def test_whatsapp_pending_and_mark_sent():
    s = fresh()
    a = make_photo("s1", "a.jpg")
    s.whatsapp_optin("+15551112222", [a])
    pending = s.whatsapp_pending()
    assert len(pending) == 1 and pending[0]["photos"] == [a]
    s.whatsapp_mark_sent("+15551112222")
    assert s.whatsapp_pending() == []                # nothing left to send
    # a re-opt-in of the SAME already-sent photo stays sent (never re-queued)
    s.whatsapp_optin("+15551112222", [a])
    assert s.whatsapp_pending() == []


def test_whatsapp_invalid_inputs():
    s = fresh()
    assert s.whatsapp_optin("123", [make_photo("s1", "a.jpg")])["ok"] is False   # bad phone
    assert s.whatsapp_optin("+15551112222", ["/etc/passwd", "/captures/x/../y"])["ok"] is False


# ---- Google Drive opt-in dedup (the group-photo case) ---------------------
def test_drive_group_photo_uploaded_once():
    s = fresh()
    group = make_photo("s2", "group.jpg")
    # two different guests each opt the SAME group photo in
    r1 = s.drive_optin([group])
    r2 = s.drive_optin([group])
    assert r1["new"] == [group]      # first opt-in enqueues it
    assert r2["new"] == []           # second is a no-op — never uploaded twice
    assert s.drive_optin([group])["new"] == []


def test_drive_uploaded_never_reoptins():
    s = fresh()
    a = make_photo("s1", "a.jpg")
    assert s.drive_optin([a])["new"] == [a]
    s.drive_mark_uploaded([a])
    # even a brand-new opt-in of an already-uploaded photo returns nothing to enqueue
    assert s.drive_optin([a])["new"] == []


def test_drive_optin_validates_paths():
    s = fresh()
    good = make_photo("s1", "a.jpg")
    r = s.drive_optin([good, "/captures/nope/missing.jpg", "../../secret"])
    assert r["new"] == [good]        # only the real capture survives
