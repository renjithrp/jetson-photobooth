"""Pytest fixtures. A fresh temp data dir is configured BEFORE importing the app so
tests never touch a real settings.json / captures folder."""
import os
import tempfile

# must be set before importing backend.config (it reads BOOTH_DATA at import time)
os.environ.setdefault("BOOTH_DATA", tempfile.mkdtemp(prefix="boothtest-"))
os.environ.setdefault("BOOTH_PORT", "8000")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.main import app  # noqa: E402


@pytest.fixture
def client():
    # context manager runs startup/shutdown; cookies are isolated per client instance
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    r = client.post("/api/login", json={"pin": "1234"})
    assert r.status_code == 200
    return client
