"""API tests: auth, secret masking, health, capture flow, access control."""
import time

from backend import config


def test_settings_secrets_masked(client):
    s = client.get("/api/settings").json()
    assert s["general"]["admin_pin"] == ""        # never exposed
    assert s["storage"]["ftp"]["password"] == ""


def test_put_settings_requires_auth(client):
    r = client.put("/api/settings", json={"general": {"booth_name": "hax"}})
    assert r.status_code == 401


def test_service_control_requires_auth(client):
    r = client.post("/api/system/service",
                    json={"service": "photobooth-kiosk", "action": "stop"})
    assert r.status_code == 401


def test_gallery_delete_requires_auth(client):
    r = client.delete("/api/gallery/whatever")
    assert r.status_code == 401


def test_login_flow(client):
    assert client.post("/api/login", json={"pin": "0000"}).status_code == 401
    assert client.get("/api/auth/check").status_code == 401
    assert client.post("/api/login", json={"pin": "1234"}).status_code == 200
    assert client.get("/api/auth/check").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/auth/check").status_code == 401


def test_authed_can_save(admin):
    r = admin.put("/api/settings", json={"general": {"booth_name": "My Event Booth"}})
    assert r.status_code == 200
    assert config.load().general.booth_name == "My Event Booth"


def test_blank_secret_is_kept(admin):
    # set a real password, then a blank update must NOT wipe it
    admin.put("/api/settings", json={"storage": {"ftp": {"password": "s3cret"}}})
    assert config.load().storage.ftp.password == "s3cret"
    admin.put("/api/settings", json={"storage": {"ftp": {"password": ""}}})
    assert config.load().storage.ftp.password == "s3cret"          # kept
    admin.put("/api/settings", json={"storage": {"ftp": {"password": "newpw"}}})
    assert config.load().storage.ftp.password == "newpw"           # changed


def test_invalid_settings_rejected(admin):
    r = admin.put("/api/settings", json={"timer": {"num_shots": "not-a-number"}})
    assert r.status_code == 400


def test_out_of_range_numbers_rejected(admin):
    # Clearing a number field in the admin UI posts 0; bounds must reject the ones
    # that would break a session instead of silently saving them.
    for payload in ({"timer": {"num_shots": 0}},
                    {"timer": {"num_shots": 999}},
                    {"preview": {"fps": 0}},
                    {"printing": {"copies": 0}}):
        assert admin.put("/api/settings", json=payload).status_code == 400
    # a valid in-range value still saves
    assert admin.put("/api/settings", json={"timer": {"num_shots": 3}}).status_code == 200


def test_delete_session_cannot_wipe_captures_root(admin):
    # Regression: the old sanitizer turned "..." into "." -> the captures ROOT,
    # so DELETE /api/gallery/... rmtree'd every photo of the event.
    root = config.captures_dir()
    keep = root / "session_20260101_120000"
    other = root / "session_20260101_130000"
    keep.mkdir(parents=True, exist_ok=True)
    other.mkdir(parents=True, exist_ok=True)
    # "..." is the dangerous one: not a dot-segment, so no client/proxy normalizes
    # it away — it reaches the handler and historically resolved to the captures
    # root. ("." / ".." get normalized by the HTTP client to other routes.)
    for bad in ("...", "..", "."):
        r = admin.delete("/api/gallery/" + bad)
        assert r.status_code != 200, f"{bad} -> {r.status_code}"
    assert root.exists() and keep.exists() and other.exists()   # nothing wiped
    # a legitimate session still deletes normally
    assert admin.delete("/api/gallery/session_20260101_120000").status_code == 200
    assert not keep.exists() and other.exists()


def test_delete_photos_requires_auth(client):
    r = client.post("/api/gallery/delete", json={"photos": ["/captures/s/a.jpg"]})
    assert r.status_code == 401


def test_delete_photos_removes_only_the_named_files(admin):
    d = config.captures_dir() / "session_20260301_101010"
    d.mkdir(parents=True, exist_ok=True)
    keep, gone = d / "shot_01.jpg", d / "shot_02.jpg"
    for f in (keep, gone):
        f.write_bytes(b"jpegish")
    r = admin.post("/api/gallery/delete",
                   json={"photos": [f"/captures/{d.name}/{gone.name}"]})
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert keep.exists() and not gone.exists()
    assert d.is_dir()                    # session kept — it still has a photo


def test_delete_photos_cannot_escape_the_captures_tree(admin):
    outside = config.data_dir() / "keep-me.txt"
    outside.write_text("not a capture")
    r = admin.post("/api/gallery/delete", json={"photos": ["/captures/../keep-me.txt"]})
    assert r.status_code != 200          # resolver refuses it — nothing to delete
    assert outside.exists()


def test_login_lockout_after_repeated_failures(client):
    from backend import main
    main._login_fails.clear()
    try:
        for _ in range(main._LOGIN_MAX_FAILS):
            assert client.post("/api/login", json={"pin": "0000"}).status_code == 401
        # locked out now — even the correct PIN is refused until the window passes
        assert client.post("/api/login", json={"pin": "1234"}).status_code == 429
    finally:
        main._login_fails.clear()   # don't leak the lockout into other tests


def test_hotspot_guests_reports_opaque_devices(client):
    """The kiosk needs to spot a newly joined phone to confirm the Wi-Fi step on
    screen — but this page is open on the LAN, so no MACs or IPs come back."""
    j = client.get("/api/hotspot/guests").json()
    assert set(j) == {"count", "devices"}
    assert j["count"] == len(j["devices"])
    for d in j["devices"]:
        assert set(d) == {"id", "seconds"}
        assert len(d["id"]) == 10 and all(c in "0123456789abcdef" for c in d["id"])
        assert isinstance(d["seconds"], int)


def test_short_download_link_survives_a_big_selection(admin):
    """The iPad's download QR points at /d, not a URL listing every photo.

    The full URL grew ~74 bytes per photo and past ~37 photos couldn't be encoded
    as a QR at all, so the guest was told the code couldn't be created.
    """
    d = config.captures_dir() / "session_20260401_120000"
    d.mkdir(parents=True, exist_ok=True)
    photos = []
    for i in range(40):
        f = d / f"shot_{i:02d}.jpg"
        f.write_bytes(b"x")
        photos.append(f"/captures/{d.name}/{f.name}")
    from backend import main
    try:
        assert admin.post("/api/download/announce", json={"photos": photos}).status_code == 200
        r = admin.get("/d", follow_redirects=False)
        assert r.status_code == 302
        # One short code resolves to the full 40-photo download.
        loc = r.headers["location"]
        assert "/api/download?" in loc and loc.count("p=") == 40
    finally:
        # single-slot module state — leaving it set breaks tests that expect none
        main._pending_dl.clear()


def test_short_download_link_never_dead_ends(client):
    """A stale code (nothing pending) still lands the guest somewhere useful."""
    r = client.get("/d", follow_redirects=False)
    assert r.status_code == 302


def test_button_press_during_countdown_cancels_it(admin, monkeypatch):
    """The remote's one working button toggles: press to start, press again to stop."""
    from backend import capture_service as cs
    admin.put("/api/settings", json={
        "camera": {"backend": "mock"}, "preview": {"source": "mock"},
        "timer": {"countdown_seconds": 5, "num_shots": 1, "review_seconds": 0}})
    monkeypatch.setattr(cs, "CLOCK_WAIT_S", 0)
    root = config.captures_dir()
    root.mkdir(parents=True, exist_ok=True)
    before = {d.name for d in root.iterdir() if d.is_dir()}

    from backend.main import service
    service.trigger_threadsafe("arduino")          # press 1: starts the countdown
    time.sleep(1.5)
    assert service.busy
    service.trigger_threadsafe("arduino")          # press 2: calls it off
    # the cancel path holds the session lock while it shows "cancelled", so give it
    # longer than that message before expecting the booth to be free again
    for _ in range(40):
        if not service.busy:
            break
        time.sleep(0.25)
    assert not service.busy
    after = {d.name for d in root.iterdir() if d.is_dir()}
    assert after == before                         # no photo, no leftover folder


def test_only_the_button_cancels_a_running_countdown(monkeypatch):
    """A pose held through the countdown must not cancel the photo it just asked for.

    Unit-level on purpose: _dispatch spawns a task, and there's no running loop here.
    """
    from backend.main import service
    started = []
    monkeypatch.setattr(service, "_spawn", lambda coro: (coro.close(), started.append(1)))
    service._counting = True
    service._cancel.clear()
    try:
        service._dispatch("gesture")
        assert not service._cancel.is_set() and started == [1]   # started, not cancelled
        service._dispatch("arduino")
        assert service._cancel.is_set() and started == [1]       # cancelled, not started
    finally:
        service._counting = False
        service._cancel.clear()


def test_health(client):
    j = client.get("/api/system/info").json()
    assert "version" in j and "disk" in j and "camera" in j
    assert "daemon_connected" in j and j["preview_url"] == "/api/preview/stream"


def test_favicon(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.content.startswith(b"\x00\x00\x01\x00")      # ICONDIR: reserved, type=icon


def test_pages_carry_the_icon(client):
    """Every page links the icon, and the assets it points at are actually served."""
    for path in ("/", "/admin", "/control", "/booth"):
        assert '/assets/icon.svg' in client.get(path).text, path
    for asset in ("icon.svg", "favicon.ico", "icon-192.png", "apple-touch-icon.png"):
        assert client.get(f"/assets/{asset}").status_code == 200, asset


def test_pages_load(client):
    for path in ("/", "/admin", "/control"):
        assert client.get(path).status_code == 200


def test_capture_held_while_clock_unsynced(admin, monkeypatch):
    # A cold-booted Jetson runs at the 1970 epoch until NTP syncs; capturing then
    # produced 1969-stamped sessions. The session must refuse instead of capturing.
    from backend import capture_service as cs
    monkeypatch.setattr(cs, "CLOCK_MIN_YEAR", 9999)   # force "clock not synced"
    monkeypatch.setattr(cs, "CLOCK_WAIT_S", 0)        # no grace wait in tests
    admin.put("/api/settings", json={
        "camera": {"backend": "mock"}, "preview": {"source": "mock"},
        "timer": {"countdown_seconds": 0, "num_shots": 1, "review_seconds": 0}})
    root = config.captures_dir()
    root.mkdir(parents=True, exist_ok=True)
    before = {d.name for d in root.iterdir() if d.is_dir()}
    assert admin.post("/api/capture").json()["ok"] is True     # accepted async…
    time.sleep(1.0)
    after = {d.name for d in root.iterdir() if d.is_dir()}
    assert after == before                                     # …but nothing captured


def test_failed_capture_leaves_no_empty_session(admin):
    # Sony backend with a missing binary -> CaptureError after the session dir is
    # created; the empty folder must be cleaned up, not left in the gallery.
    admin.put("/api/settings", json={
        "camera": {"backend": "sony", "capture_binary": "/nonexistent/boothCapture"},
        "preview": {"source": "mock"},
        "timer": {"countdown_seconds": 0, "num_shots": 1, "review_seconds": 0}})
    root = config.captures_dir()
    root.mkdir(parents=True, exist_ok=True)
    before = {d.name for d in root.iterdir() if d.is_dir()}
    assert admin.post("/api/capture").json()["ok"] is True
    time.sleep(1.0)
    after = {d.name for d in root.iterdir() if d.is_dir()}
    assert after == before          # failed capture cleaned its empty folder


def test_auto_upload_enqueues_drive_job(admin):
    from backend.sync import worker as sync_worker
    admin.put("/api/settings", json={
        "camera": {"backend": "mock"}, "preview": {"source": "mock"},
        "storage": {"gdrive": {"enabled": True, "auto_upload": True}},
        "timer": {"countdown_seconds": 0, "num_shots": 1, "review_seconds": 0}})
    before = len([j for j in sync_worker.jobs if "gdrive" in j["dests"]])
    assert admin.post("/api/capture").json()["ok"] is True
    for _ in range(40):
        gjobs = [j for j in sync_worker.jobs if "gdrive" in j["dests"]]
        if len(gjobs) > before:
            break
        time.sleep(0.2)
    assert len(gjobs) > before                      # capture queued a Drive upload
    assert gjobs[-1].get("subdir") == ""            # flat event album, not a subfolder
    # reset so later tests aren't affected
    admin.put("/api/settings", json={"storage": {"gdrive": {"enabled": False, "auto_upload": False}}})


def test_capture_flow_mock(admin):
    # mock camera + no countdown/review so the session completes fast
    admin.put("/api/settings", json={
        "camera": {"backend": "mock"},
        "preview": {"source": "mock"},
        "timer": {"countdown_seconds": 0, "num_shots": 1, "review_seconds": 0},
    })
    assert admin.post("/api/capture").json()["ok"] is True

    images = []
    for _ in range(40):                  # poll up to ~8s for the async session
        gal = admin.get("/api/gallery").json()
        if gal and gal[0]["images"]:
            images = gal[0]["images"]
            break
        time.sleep(0.2)
    assert images, "capture did not produce a photo"
    assert images[0].startswith("/captures/")
