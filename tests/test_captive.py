"""Captive-portal HTTP app (:80) routing — no camera/backend needed for these."""
from fastapi.testclient import TestClient

from backend.captive import app

client = TestClient(app)


def test_os_probes_redirect_to_guest_page():
    # Apple + Android connectivity probes must NOT get the expected response, so the
    # phone decides it's behind a portal and opens /booth in its captive browser.
    for probe in ("/hotspot-detect.html", "/generate_204", "/ncsi.txt"):
        r = client.get(probe, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "http://192.168.50.1/booth"


def test_guest_page_served_over_http():
    for path in ("/", "/booth"):
        r = client.get(path)
        assert r.status_code == 200
        assert "Find your photos" in r.text
        assert r.headers.get("cache-control") == "no-store, max-age=0"
