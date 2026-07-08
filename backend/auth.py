"""Lightweight signed-token auth for the admin/config endpoints.

A successful PIN login issues an HMAC-signed, time-limited token stored in an
HttpOnly cookie. Sensitive endpoints require a valid token. No external deps —
the signing secret is generated once and kept in the data dir (mode 600).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request

from . import config

COOKIE_NAME = "booth_session"
TOKEN_TTL_DAYS = 30


def _secret() -> bytes:
    p = config.data_dir() / "secret.key"
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
        try:
            p.chmod(0o600)
        except Exception:
            pass
    return p.read_bytes()


def make_token(ttl_days: int = TOKEN_TTL_DAYS) -> str:
    exp = int(time.time()) + ttl_days * 86400
    sig = hmac.new(_secret(), f"admin:{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}:{sig}"


def valid_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        exp_s, sig = token.split(":", 1)
        exp = int(exp_s)
    except Exception:
        return False
    if exp < time.time():
        return False
    expected = hmac.new(_secret(), f"admin:{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def check_pin(pin: str) -> bool:
    real = str(config.load().general.admin_pin)
    return bool(pin) and hmac.compare_digest(str(pin), real)


def require_auth(request: Request) -> None:
    """FastAPI dependency: 401 unless a valid session cookie is present."""
    if not valid_token(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="authentication required")
