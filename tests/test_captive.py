"""Captive-portal HTTP app (:80) routing — no camera/backend needed for these."""
from fastapi.testclient import TestClient

from backend.captive import _is_guest_route, app

client = TestClient(app)


# Guest routes that MUST be forwarded to the backend (the find-your-photos +
# sharing flow), and admin/config routes that must NOT be reachable from the
# open hotspot. This is the security boundary — keep both lists honest.
GUEST_ROUTES = [
    "/api/wifi/info", "/api/faces/find", "/api/faces/status",
    "/api/share/options", "/api/share/email", "/api/share/links",
    "/api/share/whatsapp", "/api/share/drive",
    "/api/download", "/api/download/pending",
    "/captures/session_x/1.jpg", "/thumbs/session_x/1.jpg",
    "/s/session_x",
    "/assets/icon.svg", "/assets/favicon.ico",   # static frontend files (page icons)
]
BLOCKED_ROUTES = [
    "/api/login", "/api/gallery", "/api/settings", "/api/capture",
    "/api/system/service", "/api/system/info", "/api/hotspot", "/api/wifi/scan",
    "/api/wifi/connect", "/api/test/s3", "/api/faces/groups", "/api/print",
    "/api/consent/whatsapp/pending", "/api/consent/whatsapp/sent",  # admin-only
    "/api/download/announce",   # booth-tablet only — guests must not plant downloads
    "/api/faces/find/../../login",   # traversal-style probe must not match
]


def test_guest_routes_are_allowlisted():
    for path in GUEST_ROUTES:
        assert _is_guest_route(path), f"{path} should be a guest route"


def test_admin_routes_are_not_guest_routes():
    for path in BLOCKED_ROUTES:
        assert not _is_guest_route(path), f"{path} must NOT reach the backend"


def test_blocked_api_routes_return_404_not_proxied():
    # Blocked routes are refused at the captive layer BEFORE any proxy attempt,
    # so they 404 cleanly even with no backend running behind the portal.
    for path in ("/api/login", "/api/gallery", "/api/settings",
                 "/api/system/service"):
        r = client.post(path, follow_redirects=False)
        assert r.status_code == 404, f"{path} returned {r.status_code}, expected 404"


def test_os_probes_redirect_to_guest_page():
    # Apple + Android connectivity probes must NOT get the expected response, so the
    # phone decides it's behind a portal and opens /booth in its captive browser.
    for probe in ("/hotspot-detect.html", "/generate_204", "/ncsi.txt"):
        r = client.get(probe, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "http://192.168.50.1/booth"


# iOS reports this UA inside the Wi-Fi sign-in window.
CNA_UA = {"user-agent": "CaptiveNetworkSupport-355.200.27 wispr"}


def test_captive_window_gets_the_safari_handoff():
    """The sign-in window can't open the camera, so it must not serve the selfie
    app — it gets the hand-off page with an escape into Safari instead."""
    r = client.get("/booth", headers=CNA_UA)
    assert r.status_code == 200
    assert "Open in Safari" in r.text
    assert "x-safari-http://" in r.text
    assert "Use Without Internet" in r.text        # written fallback steps
    assert 'id="selfie"' not in r.text             # the camera input is NOT here


def test_real_browser_still_gets_the_full_guest_app():
    r = client.get("/booth", headers={"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari"})
    assert r.status_code == 200
    assert 'id="selfie"' in r.text                 # camera flow intact in Safari
    assert "Open in Safari" not in r.text


def test_guest_page_served_over_http():
    for path in ("/", "/booth"):
        r = client.get(path)
        assert r.status_code == 200
        assert "Find your photos" in r.text
        assert r.headers.get("cache-control") == "no-store, max-age=0"
