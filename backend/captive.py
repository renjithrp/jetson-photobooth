"""Captive-portal HTTP app (port 80) for the guest hotspot.

Runs ALONGSIDE the main HTTPS backend (:8000). Its only jobs:

  * Fire the phone's captive-portal popup when a guest joins 'PhotoBooth'.
    With DNS hijacked to the booth (see deploy/captive-dnsmasq.conf), every OS
    connectivity-probe URL (Apple/Google/Microsoft) lands here on an unknown
    host and we 302 it to /booth -> the phone's Captive Network Assistant opens
    the find-your-photos page automatically. One QR scan joins Wi-Fi AND opens
    the photos.

  * Serve the guest page + downloads over PLAIN HTTP so the captive mini-browser
    (which rejects the backend's self-signed HTTPS cert) can load them.

  * Proxy the handful of guest routes (/api/faces/*, /api/wifi/info, /captures/*,
    /s/*) to the real backend over loopback TLS (verify=False), so there is still
    exactly ONE camera / face / NPU consumer — the main process.

Deliberately does NOT start the camera hub / triggers / watchdog. Run it as its
own service (deploy/photobooth-captive.service), as root (binds :80).
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

log = logging.getLogger("captive")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
BACKEND_ORIGIN = os.environ.get("BOOTH_BACKEND", "https://127.0.0.1:8000")
AP_IP = os.environ.get("BOOTH_AP_IP", "192.168.50.1")

# Routes handed through to the real backend over loopback TLS.
_PROXY_PREFIXES = ("/api/", "/captures/", "/thumbs/", "/s/")
# Never proxy hop-by-hop / length headers — httpx already decoded the body.
_DROP_RESP_HEADERS = {"content-encoding", "transfer-encoding", "connection",
                      "content-length", "keep-alive"}
_NO_CACHE = {"Cache-Control": "no-store, max-age=0"}

_client: httpx.AsyncClient | None = None


async def _detect_origin(configured: str) -> str:
    """The backend runs HTTP or HTTPS on :8000 depending on whether a TLS cert is
    installed. Probe the configured origin, then the other scheme, so the captive
    proxy works regardless of what BOOTH_BACKEND was set to."""
    alt = (configured.replace("https://", "http://") if configured.startswith("https://")
           else configured.replace("http://", "https://"))
    for origin in (configured, alt):
        try:
            async with httpx.AsyncClient(verify=False, timeout=3.0) as c:
                r = await c.get(origin + "/api/wifi/info")
            if r.status_code < 500:
                return origin
        except Exception:
            continue
    return configured


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client, BACKEND_ORIGIN
    BACKEND_ORIGIN = await _detect_origin(BACKEND_ORIGIN)
    _client = httpx.AsyncClient(base_url=BACKEND_ORIGIN, verify=False, timeout=60.0)
    log.info("captive portal up on :80 -> %s (AP %s)", BACKEND_ORIGIN, AP_IP)
    yield
    await _client.aclose()


app = FastAPI(title="AI Photo Booth (captive)", docs_url=None, redoc_url=None,
              lifespan=lifespan)


def _guest_page() -> FileResponse:
    return FileResponse(FRONTEND / "guest" / "index.html", headers=_NO_CACHE)


async def _proxy(request: Request, path: str) -> Response:
    """Forward a guest request to the main backend and STREAM the response back.
    Streaming (vs buffering) matters here: photos and multi-photo ZIPs are tens of MB,
    and several guests download at once — buffering them all in RAM starved the 8GB
    Jetson and added seconds of time-to-first-byte."""
    assert _client is not None
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "connection", "accept-encoding")}
    req = _client.build_request(
        request.method, httpx.URL(path=path, query=request.url.query.encode()),
        headers=headers, content=await request.body(),
    )
    try:
        upstream = await _client.send(req, stream=True)
    except httpx.HTTPError as e:
        log.warning("proxy %s %s failed: %s", request.method, path, e)
        return Response("Photo booth is starting up — please try again.",
                        status_code=502, media_type="text/plain")
    out_headers = {k: v for k, v in upstream.headers.items()
                   if k.lower() not in _DROP_RESP_HEADERS}
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                             headers=out_headers,
                             media_type=upstream.headers.get("content-type"),
                             background=BackgroundTask(upstream.aclose))


@app.api_route("/{full_path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
async def gateway(request: Request, full_path: str) -> Response:
    path = "/" + full_path
    if path.startswith(_PROXY_PREFIXES):
        return await _proxy(request, path)
    if path in ("/", "/booth"):
        return _guest_page()
    # Anything else — an OS captive probe (captive.apple.com/hotspot-detect.html,
    # connectivitycheck.gstatic.com/generate_204, www.msftconnecttest.com, …) or a
    # random host the guest's phone hit — is a non-expected response, so the phone
    # decides it's behind a portal and opens /booth in its captive browser.
    return RedirectResponse(f"http://{AP_IP}/booth", status_code=302,
                            headers=_NO_CACHE)
