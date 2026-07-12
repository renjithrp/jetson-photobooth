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
    assert set(j) == {"email", "links"}
    assert j["email"] is False          # not configured in tests


def test_share_email_requires_config(client):
    urls = _make_session("session_mail", 1)
    j = client.post("/api/share/email",
                    json={"to": "a@b.co", "photos": urls}).json()
    assert j["ok"] is False             # email disabled by default


def test_share_links_requires_cloud(client):
    urls = _make_session("session_links", 1)
    j = client.post("/api/share/links", json={"photos": urls}).json()
    assert j["ok"] is False and "cloud" in j["error"].lower() or "destination" in j["error"]
