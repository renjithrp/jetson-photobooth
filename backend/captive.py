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
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

log = logging.getLogger("captive")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
BACKEND_ORIGIN = os.environ.get("BOOTH_BACKEND", "https://127.0.0.1:8000")
AP_IP = os.environ.get("BOOTH_AP_IP", "192.168.50.1")
# Friendly name resolved for guests by the hotspot's dnsmasq (deploy/booth-hostname.conf).
# Shown to humans; the IP stays the fallback for phones on encrypted DNS, which
# bypass the booth's resolver entirely and can never resolve it.
AP_HOST = os.environ.get("BOOTH_AP_HOST", "photos.internal")

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
# "/assets/" is the static frontend mount (icons, tailwind.js) -- inert files,
# GET-only, and strictly less sensitive than the guest photos already proxied
# below. The guest page needs it for its icons.
_PROXY_PREFIXES = ("/captures/", "/thumbs/", "/s/", "/assets/")
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


# Booth-owned devices (the kiosk iPad) whose captive probes get a SUCCESS answer so
# iOS never pops the sign-in sheet over the kiosk app. Comma-separated IPs; give the
# device a dnsmasq reservation so its IP is stable.
_KIOSK_IPS = {ip.strip() for ip in os.environ.get("BOOTH_KIOSK_IPS", "").split(",") if ip.strip()}
_APPLE_SUCCESS = "<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"

# BOOTH_CAPTIVE_MODE=silent answers every guest's connectivity probe with the
# expected "success", so the phone treats the hotspot as an ordinary network:
# no sign-in sheet at all, and no "no internet" warning — the same effect as the
# guest tapping "Use Without Internet", which a portal cannot select for them.
#
# The cost is the automatic entry point: with no sheet, a guest who has photos
# waiting is never shown them, so they must scan the kiosk QR instead. Default
# stays "sheet" for that reason.
_SILENT_CAPTIVE = os.environ.get("BOOTH_CAPTIVE_MODE", "sheet").strip().lower() == "silent"


# iOS's Captive Network Assistant identifies itself here; Android's captive login
# webview does the same with its own marker. Anything else is a real browser.
_CNA_MARKERS = ("captivenetworksupport", "captiveportallogin")


def _is_captive_browser(request: Request) -> bool:
    ua = request.headers.get("user-agent", "").lower()
    return any(m in ua for m in _CNA_MARKERS)


async def _pending() -> dict:
    """What the booth has queued for this guest, or {} if the lookup fails.

    Fresh client on purpose: the shared pool's keep-alive sockets go stale when the
    backend restarts, and a silently-failed lookup here served the popup WITHOUT the
    download button (observed live). One tiny loopback request per load — guests join
    rarely, correctness wins.
    """
    try:
        async with httpx.AsyncClient(base_url=BACKEND_ORIGIN, verify=False,
                                     timeout=2.0) as c:
            return (await c.get("/api/download/pending")).json()
    except Exception:
        log.warning("pending-download lookup failed")
        return {}


async def _captive_landing() -> HTMLResponse:
    """Hand the guest off to Safari.

    The sign-in window cannot open the camera, so the selfie ("find my photos")
    flow is dead in here — it silently does nothing when tapped. Rather than serve
    the full app and let guests hit that wall, this page explains the situation and
    offers a one-tap escape into a real browser, where the camera works.

    A download the booth already queued for this guest still works right here (it's
    just a link), so that keeps its button and is NOT pushed out to Safari.
    """
    import html as html_lib
    text = (FRONTEND / "guest" / "captive.html").read_text()
    j = await _pending()
    disp, title, url = "none", "", "#"
    if j.get("pending"):
        disp = "block"
        title = f"Your {j['count']} photo{'s' if j['count'] > 1 else ''} are ready"
        url = html_lib.escape(j["download"], quote=True)
    # x-safari-http:// is the scheme that breaks out of the captive window into
    # Safari on iOS. If the OS doesn't honour it the page's written steps cover it.
    text = (text.replace("__PENDING_DISPLAY__", disp)
                .replace("__PENDING_TITLE__", title)
                .replace("__PENDING_URL__", url)
                # The Safari hand-off uses the IP on purpose: it must work even
                # when the guest's phone can't resolve the booth's private name.
                .replace("__SAFARI_URL__", f"x-safari-http://{AP_IP}/booth")
                .replace("__BOOTH_HOST__", AP_HOST)
                .replace("__BOOTH_IP__", AP_IP))
    return HTMLResponse(text, headers=_NO_CACHE)


async def _guest_page() -> HTMLResponse:
    """Serve the guest page with the ready-to-download banner rendered SERVER-SIDE
    (captive mini-browsers don't reliably run JavaScript — observed live on iOS)."""
    import html as html_lib
    text = (FRONTEND / "guest" / "index.html").read_text()
    disp, title, url = "none", "", "#"
    j = await _pending()
    if j.get("pending"):
        disp = "block"
        title = f"Your {j['count']} photo{'s' if j['count'] > 1 else ''} are ready"
        url = html_lib.escape(j["download"], quote=True)
    text = (text.replace("__PENDING_DISPLAY__", disp)
                .replace("__PENDING_TITLE__", title)
                .replace("__PENDING_URL__", url))
    return HTMLResponse(text, headers=_NO_CACHE)


async def _proxy(request: Request, path: str) -> Response:
    """Forward a guest request to the main backend and STREAM the response back.
    Streaming (vs buffering) matters here: photos and multi-photo ZIPs are tens of MB,
    and several guests download at once — buffering them all in RAM starved the 8GB
    Jetson and added seconds of time-to-first-byte."""
    assert _client is not None
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "connection", "accept-encoding")}
    body = await request.body()
    upstream = None
    # One retry: after a backend restart the pool's keep-alive sockets are stale and
    # the first request dies with a connection-level error; a fresh attempt succeeds.
    for attempt in (1, 2):
        req = _client.build_request(
            request.method, httpx.URL(path=path, query=request.url.query.encode()),
            headers=headers, content=body,
        )
        try:
            upstream = await _client.send(req, stream=True)
            break
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as e:
            if attempt == 2:
                log.warning("proxy %s %s failed: %s", request.method, path, e)
                return Response("Photo booth is starting up — please try again.",
                                status_code=502, media_type="text/plain")
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
    # /welcome is where the OS connectivity probes are redirected, so it is only
    # ever loaded by the Wi-Fi sign-in window. Keying off the URL rather than the
    # user agent is what makes this reliable: UA sniffing was tried first and the
    # booth's own phones fell straight through it and got the camera app in a
    # window that cannot open a camera.
    if path == "/welcome":
        log.info("captive window: %s", request.headers.get("user-agent", "?"))
        return await _captive_landing()
    if path in ("/", "/booth"):
        # A captive browser that reaches the app directly still gets the hand-off.
        if _is_captive_browser(request):
            return await _captive_landing()
        return await _guest_page()
    # The kiosk iPad must never see the captive sign-in sheet: answer its OS
    # connectivity probes with the expected "success" so iOS treats the hotspot
    # as a normal network. (Guests fall through to the redirect below.)
    if _SILENT_CAPTIVE or (request.client and request.client.host in _KIOSK_IPS):
        if "generate_204" in path:
            return Response(status_code=204)
        return HTMLResponse(_APPLE_SUCCESS)
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
    return RedirectResponse(f"http://{AP_IP}/welcome", status_code=302,
                            headers=_NO_CACHE)
