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


def test_health(client):
    j = client.get("/api/system/info").json()
    assert "version" in j and "disk" in j and "camera" in j
    assert "daemon_connected" in j and j["preview_url"] == "/api/preview/stream"


def test_favicon(client):
    assert client.get("/favicon.ico").status_code == 204


def test_pages_load(client):
    for path in ("/", "/admin", "/control"):
        assert client.get(path).status_code == 200


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
