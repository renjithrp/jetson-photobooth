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
#
# SECURITY: guests on the open hotspot reach ONLY these routes. This used to be a
# blanket "/api/" prefix, which silently exposed the ENTIRE backend API to every
# guest — including /api/login (brute-forceable admin PIN), /api/gallery (list &
# download every guest's photos), /api/settings (config/credential recon) and
# /api/capture, /api/system/service, /api/hotspot, /api/wifi/*, /api/test/*.
# The captive portal must forward the find-your-photos + sharing flow and nothing
# else, so this is an explicit allowlist. Static photo trees (/captures, /thumbs)
# and share pages (/s) are prefixes; the guest API routes are matched exactly so a
# path like "/api/faces/find/../../login" can't sneak through.
_PROXY_PREFIXES = ("/captures/", "/thumbs/", "/s/")
_PROXY_EXACT = frozenset({
    "/api/wifi/info",        # hotspot details + QR (also the origin-probe route)
    "/api/faces/find",       # find my photos by selfie
    "/api/faces/status",     # whether find-my-photos is available
    "/api/share/options",    # which share buttons to show (email / links)
    "/api/share/email",      # email my photos
    "/api/share/links",      # public cloud links (WhatsApp)
    "/api/share/whatsapp",   # leave a phone number for WhatsApp delivery
    "/api/share/drive",      # opt a photo in for Google Drive upload
    "/api/download",         # zip download of selected photos
    "/api/download/pending", # ready-to-download banner on the captive popup page
})


def _is_guest_route(path: str) -> bool:
    return path in _PROXY_EXACT or path.startswith(_PROXY_PREFIXES)
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
    if _is_guest_route(path):
        return await _proxy(request, path)
    if path in ("/", "/booth"):
        return _guest_page()
    # Guests must never reach admin/config/control routes — anything under /api/
    # that isn't an allowlisted guest route is refused here (not redirected, so a
    # scripted probe gets a clean 404 rather than the guest page).
    if path.startswith("/api/"):
        return Response("Not found.", status_code=404, media_type="text/plain",
                        headers=_NO_CACHE)
    # Anything else — an OS captive probe (captive.apple.com/hotspot-detect.html,
    # connectivitycheck.gstatic.com/generate_204, www.msftconnecttest.com, …) or a
    # random host the guest's phone hit — is a non-expected response, so the phone
    # decides it's behind a portal and opens /booth in its captive browser.
    return RedirectResponse(f"http://{AP_IP}/booth", status_code=302,
                            headers=_NO_CACHE)
