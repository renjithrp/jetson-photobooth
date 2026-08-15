"""Guest sharing: thumbnails, ZIP download, email/link endpoints, path safety."""
import io
import zipfile

from PIL import Image

from backend import config


def _make_session(name="session_test", n=2):
    d = config.captures_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    urls = []
    for i in range(n):
        p = d / f"photo_{i}.jpg"
        Image.new("RGB", (800, 600), (i * 40, 100, 200)).save(p, "JPEG")
        urls.append(f"/captures/{name}/{p.name}")
    return urls


def test_thumb_generated_and_cached(client):
    (url,) = _make_session("session_thumb", 1)
    r = client.get(url.replace("/captures/", "/thumbs/"))
    assert r.status_code == 200
    im = Image.open(io.BytesIO(r.content))
    assert max(im.size) <= 480
    # cached second hit
    assert client.get(url.replace("/captures/", "/thumbs/")).status_code == 200


def test_thumb_rejects_escape(client):
    assert client.get("/thumbs/../settings.json").status_code in (404, 400)
    assert client.get("/thumbs/nope/nothing.jpg").status_code == 404


def test_download_zip_multi(client):
    urls = _make_session("session_zip", 3)
    r = client.get("/api/download", params=[("p", u) for u in urls])
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert len(z.namelist()) == 3


def test_download_rejects_bad_paths(client):
    r = client.get("/api/download", params=[("p", "/etc/passwd"),
                                            ("p", "/captures/../settings.json")])
    assert r.status_code == 404


def test_share_options_shape(client):
    j = client.get("/api/share/options").json()
    assert set(j) == {"email", "links", "whatsapp", "drive_optin"}
    assert j["email"] is False          # not configured in tests
    assert j["whatsapp"] is False and j["drive_optin"] is False


def test_whatsapp_optin_collect_and_admin_flow(admin):
    admin.put("/api/settings", json={"share": {"whatsapp_optin": True}})
    urls = _make_session("session_wa", 2)
    # guest opts in with a phone + their photos
    r = admin.post("/api/share/whatsapp", json={"phone": "+1 555 010 2020", "photos": urls})
    assert r.json()["ok"] and r.json()["added"] == 2
    # opting in again with the same photos adds nothing (dedup)
    assert admin.post("/api/share/whatsapp",
                      json={"phone": "15550102020", "photos": urls}).json()["added"] == 0
    # admin sees one pending recipient with a wa.me link
    pend = admin.get("/api/consent/whatsapp/pending").json()
    assert pend["count"] == 1
    assert pend["pending"][0]["wa_link"].startswith("https://wa.me/15550102020")
    # mark sent -> nothing pending
    assert admin.post("/api/consent/whatsapp/sent", json={"phone": "15550102020"}).json()["ok"]
    assert admin.get("/api/consent/whatsapp/pending").json()["count"] == 0


def test_whatsapp_optin_refused_when_disabled(admin):
    admin.put("/api/settings", json={"share": {"whatsapp_optin": False}})
    urls = _make_session("session_wa2", 1)
    r = admin.post("/api/share/whatsapp", json={"phone": "+15550102020", "photos": urls})
    assert r.json()["ok"] is False          # opt-in refused unless enabled in settings


def test_drive_optin_dedup_group_photo(admin):
    admin.put("/api/settings", json={
        "share": {"drive_optin": True},
        "storage": {"gdrive": {"enabled": True}}})
    (group,) = _make_session("session_grp", 1)
    assert admin.post("/api/share/drive", json={"photos": [group]}).json()["added"] == 1
    # a second guest opting the same group photo in adds nothing (uploaded once)
    assert admin.post("/api/share/drive", json={"photos": [group]}).json()["added"] == 0


def test_share_email_requires_config(client):
    urls = _make_session("session_mail", 1)
    j = client.post("/api/share/email",
                    json={"to": "a@b.co", "photos": urls}).json()
    assert j["ok"] is False             # email disabled by default


def test_share_links_requires_cloud(client):
    urls = _make_session("session_links", 1)
    j = client.post("/api/share/links", json={"photos": urls}).json()
    assert j["ok"] is False and "cloud" in j["error"].lower() or "destination" in j["error"]
