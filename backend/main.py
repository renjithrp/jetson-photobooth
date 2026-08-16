"""FastAPI application: serves the kiosk + admin UIs and the booth API."""
from __future__ import annotations

import asyncio
import base64
import copy
import functools
import io
import json
import logging
from contextlib import asynccontextmanager
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import datetime
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

import qrcode

from fastapi import (Depends, FastAPI, File, HTTPException, Query, Request, Response,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import (auth, config, consent, face_index, faces, gestures, liveview, printing,
               share, uploaders, wifi)
from .sync import worker as sync_worker
from .auth import require_auth
from .faces import make_face_engine
from .camera import make_camera
from .capture_service import CaptureService
from .events import bus
from .sony_hub import hub
from .triggers import TriggerManager
from .watchdog import CameraWatchdog

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("booth")

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
PORT = int(os.environ.get("BOOTH_PORT", "8000"))
SCHEME = os.environ.get("BOOTH_SCHEME", "http")   # set to "https" when TLS is enabled


def get_lan_ip() -> str:
    """Best-effort primary LAN IP (no traffic actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def base_url() -> str:
    return f"{SCHEME}://{get_lan_ip()}:{PORT}"


def guest_base_url() -> str:
    """Guest-facing base URL — the hotspot URL (share.base_url) if set, else the LAN IP."""
    return (config.load().share.base_url or base_url()).rstrip("/")


@functools.lru_cache(maxsize=32)
def _qr_data_uri(text: str) -> str:
    """Render `text` as a QR PNG and return it as a data: URI (for inline <img>).
    Cached — the kiosk polls /api/wifi/info every 30s and the QR text rarely changes."""
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    bus.bind_loop(loop)
    service.bind_loop(loop)
    app.mount("/captures", StaticFiles(directory=str(config.captures_dir())), name="captures")
    s = config.load()
    # Load numpy + OpenCV once here, in the main thread, BEFORE any worker thread
    # spawns. numpy 2.x raises "cannot load module more than once per process" if its
    # C extension is first imported concurrently from two threads (the faces warmup
    # thread vs. a capture's executor thread), which intermittently broke face grouping
    # until a restart. Eager main-thread import makes every later import a cached no-op.
    try:
        import numpy  # noqa: F401
        import cv2     # noqa: F401
    except Exception as e:
        log.warning("eager numpy/cv2 preload skipped: %s", e)
    sony = s.preview.source == "sony_http"
    if sony:
        hub.configure(s, service.trigger_threadsafe)   # single camera consumer + gesture
        hub.start()
    triggers.start(s, skip_gesture=sony)               # hub handles gesture for Sony
    watchdog.start()                                   # self-heals a wedged camera daemon
    sync_worker.start()                                # background uploads (offline-safe queue)
    if s.faces.enabled:                                # load the face model off the boot path
        threading.Thread(target=faces.warmup, args=(s,), daemon=True).start()
    if s.ai.enabled:                                   # pre-load/fetch the segmentation model
        from . import ai_effects
        threading.Thread(target=ai_effects.warmup, args=(s,), daemon=True).start()
    if s.gaze.enabled:                                 # pre-load the gaze detector (measure scaffold)
        from . import gaze_effects
        threading.Thread(target=gaze_effects.warmup, args=(s,), daemon=True).start()
    log.info("ready at %s (admin: %s/admin)", base_url(), base_url())
    yield
    watchdog.stop()
    triggers.stop()
    hub.stop()
    sync_worker.stop()


app = FastAPI(title="PhotoBooth Pro", version="1.0.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=str(FRONTEND / "assets")), name="assets")
service = CaptureService(base_url)
triggers = TriggerManager(on_trigger=service.trigger_threadsafe,
                          on_print=service.print_last_threadsafe)
watchdog = CameraWatchdog(hub, config, lambda: service.busy)


# ---- UI pages -------------------------------------------------------------
_NO_CACHE = {"Cache-Control": "no-store, max-age=0"}


@app.get("/", response_class=HTMLResponse)
async def kiosk() -> FileResponse:
    return FileResponse(FRONTEND / "kiosk" / "index.html", headers=_NO_CACHE)


@app.get("/admin", response_class=HTMLResponse)
async def admin() -> FileResponse:
    return FileResponse(FRONTEND / "admin" / "index.html", headers=_NO_CACHE)


@app.get("/control", response_class=HTMLResponse)
async def control() -> FileResponse:
    return FileResponse(FRONTEND / "control" / "index.html", headers=_NO_CACHE)


@app.get("/booth", response_class=HTMLResponse)
async def booth() -> FileResponse:
    """Public guest page: find-your-photos by selfie + download (offline/hotspot mode)."""
    return FileResponse(FRONTEND / "guest" / "index.html", headers=_NO_CACHE)


@app.get("/api/wifi/info")
async def wifi_info() -> dict:
    """Guest hotspot details + scannable QR codes for the kiosk/guest pages.

    `join_qr` is a standard Wi-Fi QR (most phones offer one-tap join on scan);
    `find_qr`/`find_url` point at the guest find-your-photos page.
    """
    hp = wifi.hotspot_status()
    find_url = f"{guest_base_url()}/booth"
    out = {**hp, "find_url": find_url, "find_qr": _qr_data_uri(find_url)}
    if hp.get("active") and hp.get("ssid"):
        # WIFI: URI scheme — T=auth type, S=ssid, P=passphrase, H=hidden. WPA2-PSK uses "WPA".
        # H:true is REQUIRED for hidden SSIDs or the phone won't probe/join from the QR.
        h = "H:true;" if hp.get("hidden") else ""
        join = f"WIFI:T:WPA;S:{hp['ssid']};P:{hp.get('password', '')};{h};"
        out["join_qr"] = _qr_data_uri(join)
    return out


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


# ---- session-path safety --------------------------------------------------
def _session_dir(session: str) -> Path:
    """Resolve a capture-session folder name to a path INSIDE the captures dir.

    Replaces three copies of an ad-hoc ``.replace("/","").replace("..","")``
    sanitizer that was both traversal-fragile and — for ``delete_session`` —
    dangerous: inputs like ``"..."`` collapsed to ``"."`` and resolved to the
    captures ROOT, so an rmtree there wiped the whole event. Resolve the real
    path and require it to be a DIRECT child of the captures dir (never the root
    itself, never an ancestor), raising 400 otherwise."""
    root = config.captures_dir().resolve()
    d = (root / session).resolve()
    if d == root or d.parent != root:
        raise HTTPException(400, "invalid session")
    return d


# ---- auth -----------------------------------------------------------------
def _mask_secrets(data: dict) -> dict:
    """Never expose the admin PIN or stored passwords over the API."""
    d = copy.deepcopy(data)
    try:
        d["general"]["admin_pin"] = ""
    except Exception:
        pass
    try:
        d["storage"]["ftp"]["password"] = ""
    except Exception:
        pass
    for path in (["storage", "gdrive", "client_secret"], ["storage", "gdrive", "token"],
                 ["storage", "s3", "secret_access_key"], ["share", "email", "smtp_password"]):
        try:
            node = d
            for k in path[:-1]:
                node = node[k]
            node[path[-1]] = ""
        except Exception:
            pass
    try:
        d["network"]["hotspot_password"] = ""
    except Exception:
        pass
    return d


def _strip_blank_secret(partial: dict, path: list[str]) -> None:
    """Drop a secret from an incoming update if blank (UI sends '' = keep current)."""
    d = partial
    for k in path[:-1]:
        if not isinstance(d, dict) or k not in d:
            return
        d = d[k]
    if isinstance(d, dict) and d.get(path[-1], None) in ("", None):
        d.pop(path[-1], None)


# Brute-force guard for the admin PIN. The captive proxy no longer exposes
# /api/login to the hotspot, but this is defense-in-depth for anyone on the
# management LAN: after too many failures from one IP we lock that IP out for a
# growing window, so a 4-digit PIN can't be exhausted by scripting.
_login_fails: dict[str, list] = {}      # ip -> [fail_count, locked_until_ts]
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_S = 60


@app.post("/api/login")
async def login(body: dict, request: Request, response: Response) -> dict:
    ip = request.client.host if request.client else "?"
    now = time.time()
    rec = _login_fails.get(ip)
    if rec and rec[1] > now:
        raise HTTPException(429, f"too many attempts — wait {int(rec[1] - now)}s")
    if not auth.check_pin(str(body.get("pin", ""))):
        # count the failure and, past the threshold, lock this IP out for a window
        # that doubles each further failure (60s, 120s, 240s, …, capped at 1h).
        n = (rec[0] if rec else 0) + 1
        lock = now + min(_LOGIN_LOCK_S * 2 ** max(0, n - _LOGIN_MAX_FAILS), 3600) \
            if n >= _LOGIN_MAX_FAILS else 0
        _login_fails[ip] = [n, lock]
        if len(_login_fails) > 1000:      # bound the map on a long-running booth
            _login_fails.clear()
        await asyncio.sleep(0.5)          # blunt rapid online guessing
        raise HTTPException(401, "invalid PIN")
    _login_fails.pop(ip, None)            # clear on success
    response.set_cookie(auth.COOKIE_NAME, auth.make_token(), httponly=True,
                        samesite="lax", max_age=auth.TOKEN_TTL_DAYS * 86400)
    log.info("admin login")
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/check")
async def auth_check(_: None = Depends(require_auth)) -> dict:
    return {"ok": True}


@app.get("/s/{session}", response_class=HTMLResponse)
async def share_page(session: str) -> HTMLResponse:
    sess = _session_dir(session)
    safe = sess.name
    if not sess.is_dir():
        raise HTTPException(404, "session not found")
    imgs = sorted(p.name for p in sess.iterdir()
                  if p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.name != "qr.png")
    items = "".join(
        f'<a href="/captures/{safe}/{n}" download><img src="/thumbs/{safe}/{n}" loading="lazy"></a>'
        for n in imgs)
    zip_qs = "&".join("p=" + urllib.parse.quote(f"/captures/{safe}/{n}") for n in imgs)
    html = f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Your Photos</title>
<style>body{{font-family:system-ui;background:#111;color:#eee;text-align:center;margin:0;padding:16px}}
img{{max-width:100%;border-radius:12px;margin:8px 0;box-shadow:0 6px 24px #0008}}
h1{{font-weight:600}} a{{display:block}}
.dl{{display:inline-block;background:#0d9488;color:#fff;text-decoration:none;font-weight:600;
padding:12px 22px;border-radius:12px;margin:6px 0 14px}}</style>
<h1>Your Photos</h1>
<a class="dl" href="/api/download?{zip_qs}">Download all ({len(imgs)})</a>
<p>Or tap an image to download it alone.</p>{items}"""
    return HTMLResponse(html)


# ---- system / settings ----------------------------------------------------
@app.get("/api/system/info")
async def system_info() -> dict:
    s = config.load()
    cam = make_camera(s)
    # For the Sony live-view (its own MJPEG server) the kiosk connects DIRECTLY to
    # avoid proxying a long-lived stream through the backend (which leaks connections
    # and exhausts the CrSDK server's thread pool). Mock/webcam are generated in the
    # backend, so those still go through /api/preview/stream.
    # All preview now flows through the backend (the Sony hub buffers frames),
    # so it's same-origin and there's no cross-origin / leak problem.
    du = shutil.disk_usage(str(config.captures_dir()))
    daemon = hub.connected if s.preview.source == "sony_http" else None
    return {
        "version": app.version,
        "hostname": socket.gethostname(),
        "ip": get_lan_ip(),
        "port": PORT,
        "admin_url": f"{base_url()}/admin",
        "busy": service.busy,
        "camera": cam.status(),
        "trigger_mode": s.trigger.mode,
        "require_face": s.trigger.require_face,
        "face_zone": dict(zip(("x", "y", "w", "h"), gestures.region_box(s.trigger))),
        "preview_url": "/api/preview/stream",
        "preview_enabled": s.preview.enabled,
        "gdrive_connected": bool(s.storage.gdrive.token),
        "daemon_connected": daemon,
        "camera_stream": hub.health() if s.preview.source == "sony_http" else None,
        "watchdog_restarts": watchdog.restarts,
        "disk": {
            "free_gb": round(du.free / 1e9, 1),
            "total_gb": round(du.total / 1e9, 1),
            "used_pct": round(100 * du.used / du.total),
        },
    }


@app.get("/api/settings")
async def get_settings() -> dict:
    return _mask_secrets(config.load().model_dump())


@app.put("/api/settings")
async def put_settings(partial: dict, _: None = Depends(require_auth)) -> dict:
    # blank secrets mean "keep current" (the UI never receives the real values)
    _strip_blank_secret(partial, ["general", "admin_pin"])
    _strip_blank_secret(partial, ["storage", "ftp", "password"])
    _strip_blank_secret(partial, ["storage", "gdrive", "client_secret"])
    _strip_blank_secret(partial, ["storage", "gdrive", "token"])
    _strip_blank_secret(partial, ["storage", "s3", "secret_access_key"])
    _strip_blank_secret(partial, ["share", "email", "smtp_password"])
    _strip_blank_secret(partial, ["network", "hotspot_password"])
    old = config.load()
    try:
        s = config.update(partial)
    except Exception as e:
        raise HTTPException(400, f"invalid settings: {e}")
    # only re-init camera/preview/triggers when those actually changed (no preview blip)
    if old.preview != s.preview or old.trigger != s.trigger or old.camera != s.camera:
        sony = s.preview.source == "sony_http"
        if sony:
            hub.restart(s, service.trigger_threadsafe)
        else:
            hub.stop()
        triggers.restart(s, skip_gesture=sony)
        log.info("camera/trigger settings changed -> reinitialised")
    return _mask_secrets(s.model_dump())


# ---- capture / preview ----------------------------------------------------
_bg_tasks: set[asyncio.Task] = set()   # strong refs so the loop can't GC-cancel these


@app.post("/api/capture")
async def manual_capture() -> dict:
    if service.busy:
        return {"ok": False, "reason": "busy"}
    task = asyncio.create_task(service.run_session("manual"))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"ok": True}


@app.post("/api/focus")
async def manual_focus() -> dict:
    """Trigger autofocus via the camera daemon (for the mobile control page)."""
    s = config.load()
    if not (s.camera.backend == "sony" and s.preview.source == "sony_http"):
        return {"ok": False, "reason": "autofocus needs the Sony camera daemon"}
    base = (s.preview.sony_http_url or "http://127.0.0.1:8080/").rstrip("/")
    loop = asyncio.get_running_loop()

    def _focus() -> bytes:
        with urllib.request.urlopen(base + "/focus", timeout=8) as r:  # close the socket
            return r.read()
    try:
        raw = await loop.run_in_executor(None, _focus)
        return json.loads(raw.decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Real systemd units the admin UI may control. NOTE: there is deliberately no
# "photobooth-kiosk" here — the kiosk is a GNOME autostart entry, not a service,
# so controlling it via systemctl always failed while the UI reported success.
_SERVICES = {"photobooth", "photobooth-camera", "photobooth-captive",
             "photobooth-gesture"}
_ACTIONS = {"start", "stop", "restart"}


@app.post("/api/system/service")
async def service_control(body: dict, _: None = Depends(require_auth)) -> dict:
    """Start/stop/restart booth services from the admin UI (app runs as root).

    Reports the REAL systemctl outcome. Restarting the backend itself is the one
    case that must be detached — it kills this request — so that returns
    optimistically; every other unit runs synchronously and returns the true
    exit status so a failed restart shows as a failure instead of a false ✓."""
    svc, action = body.get("service"), body.get("action")
    if svc not in _SERVICES or action not in _ACTIONS:
        raise HTTPException(400, "invalid service or action")
    unit = f"{svc}.service"
    if svc == "photobooth":
        # Detach: this restart tears down the process serving the request.
        subprocess.Popen(["sh", "-c", f"sleep 1; systemctl {action} {unit}"])
        return {"ok": True, "detached": True, "service": svc, "action": action}
    loop = asyncio.get_running_loop()
    r = await loop.run_in_executor(None, lambda: subprocess.run(
        ["systemctl", action, unit], capture_output=True, text=True, timeout=30))
    if r.returncode != 0:
        return {"ok": False, "service": svc, "action": action,
                "error": (r.stderr or "systemctl failed").strip()}
    return {"ok": True, "service": svc, "action": action}


@app.get("/api/preview/stream")
async def preview_stream(request: Request) -> StreamingResponse:
    s = config.load()
    media = "multipart/x-mixed-replace; boundary=frame"
    # Sony preview comes from the hub's shared buffer; mock/webcam render per-client.
    # The async stream stops when the client disconnects (no leaked threads/sockets).
    if s.preview.source == "sony_http":
        return StreamingResponse(liveview.stream(request, hub.get_latest, s.preview.fps),
                                 media_type=media)
    src = liveview.make_source(s)
    return StreamingResponse(liveview.stream(request, src.read_jpeg, s.preview.fps, src.close),
                             media_type=media)


# ---- faces (group photos by person) --------------------------------------
@app.get("/api/faces/status")
async def faces_status() -> dict:
    s = config.load()
    ok, detail = make_face_engine(s).available()
    provs = faces.active_providers()
    gpu = any("CUDA" in p or "Tensorrt" in p for p in provs)
    return {"enabled": s.faces.enabled, "engine": s.faces.engine,
            "available": ok, "detail": detail,
            "providers": provs, "gpu": gpu, "loaded": bool(provs),
            **face_index.index.stats()}


@app.get("/api/faces/groups")
async def faces_groups(_: None = Depends(require_auth)) -> list[dict]:
    return face_index.index.groups()


@app.post("/api/faces/find")
async def faces_find(selfie: UploadFile = File(...)) -> dict:
    s = config.load()
    if not (s.faces.enabled and s.faces.allow_guest_find):
        raise HTTPException(403, "find-my-photos is disabled")
    eng = make_face_engine(s)
    ok, detail = eng.available()
    if not ok:
        return {"matched": False, "error": detail}
    # unique temp file — concurrent guests must not overwrite each other's selfie
    fd, name = tempfile.mkstemp(dir=str(config.data_dir()), prefix="_find_", suffix=".jpg")
    tmp = Path(name)
    loop = asyncio.get_running_loop()
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(await selfie.read())
        embs = await loop.run_in_executor(None, lambda: eng.embed_image(tmp))
    finally:
        tmp.unlink(missing_ok=True)
    if not embs:
        return {"matched": False, "error": "no face detected — try again"}
    res = face_index.index.match(embs[0], s.faces.match_threshold)
    # Drop any matched photos whose files no longer exist (deleted sessions leave
    # stale face-index entries), so guests never see broken/404 thumbnails.
    if res.get("photos"):
        res["photos"] = [u for u in res["photos"] if share.resolve_capture(u) is not None]
        res["matched"] = bool(res["photos"])
    return res


# ---- guest sharing (thumbnails / zip download / email / links) -------------
@app.get("/thumbs/{rest:path}")
async def thumb(rest: str) -> FileResponse:
    """Small cached thumbnail for a capture — keeps guest grids fast on hotspot Wi-Fi."""
    loop = asyncio.get_running_loop()
    p = await loop.run_in_executor(None, lambda: share.thumb_for(f"/captures/{rest}"))
    if p is None:
        raise HTTPException(404, "not found")
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/download")
async def download_zip(p: list[str] = Query([])) -> FileResponse:
    """Single-click download of the selected photos as one ZIP."""
    files = share.resolve_photos(p)
    if not files:
        raise HTTPException(404, "no valid photos selected")
    loop = asyncio.get_running_loop()
    zpath = await loop.run_in_executor(None, lambda: share.build_zip(files))
    name = "photos_" + datetime.datetime.now().strftime("%Y%m%d_%H%M") + ".zip"
    return FileResponse(zpath, media_type="application/zip", filename=name,
                        background=BackgroundTask(zpath.unlink, missing_ok=True))


@app.get("/api/share/options")
async def share_options() -> dict:
    """What the guest page can offer: email, public links (WhatsApp), collect-my-
    number for WhatsApp, and opt-in-to-Google-Drive."""
    s = config.load()
    st = s.storage
    e = s.share.email
    return {
        "email": bool(e.enabled and e.smtp_host and e.smtp_user),
        "links": bool((st.s3.enabled and st.s3.bucket and st.s3.access_key_id)
                      or (st.gdrive.enabled and st.gdrive.token)),
        "whatsapp": bool(s.share.whatsapp_optin),
        "drive_optin": bool(s.share.drive_optin and st.gdrive.enabled),
    }


@app.post("/api/share/whatsapp")
async def share_whatsapp(body: dict) -> dict:
    """Guest opt-in: store a phone number + the guest's photos for later WhatsApp
    delivery (collect-only; the admin sends when back online). Deduped."""
    if not config.load().share.whatsapp_optin:
        return {"ok": False, "error": "WhatsApp opt-in is disabled"}
    return consent.store.whatsapp_optin(str(body.get("phone", "")),
                                        list(body.get("photos") or []))


@app.post("/api/share/drive")
async def share_drive(body: dict) -> dict:
    """Guest opt-in: mark a set of photos for Google Drive upload. Each photo is
    enqueued at most once (group photos opted in by several guests upload once)."""
    s = config.load()
    if not (s.share.drive_optin and s.storage.gdrive.enabled):
        return {"ok": False, "error": "Google Drive opt-in is disabled"}
    r = consent.store.drive_optin(list(body.get("photos") or []))
    if r.get("new"):
        files = share.resolve_photos(r["new"])
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: sync_worker.enqueue_drive(files, s))
    return {"ok": r["ok"], "added": r.get("added", 0), "error": r.get("error")}


@app.get("/api/consent/whatsapp/pending")
async def whatsapp_pending(_: None = Depends(require_auth)) -> dict:
    """Admin send console: recipients with photos not yet sent, each with a wa.me
    click-to-chat link. NOTE: wa.me can only open a chat with prefilled text — the
    photos travel as the download link below, which the guest must be able to reach
    (booth network, or a public Drive/S3 link once uploaded)."""
    base = guest_base_url()
    booth = config.load().general.booth_name
    out = []
    for r in consent.store.whatsapp_pending():
        qs = "&".join("p=" + urllib.parse.quote(u) for u in r["photos"])
        dl = f"{base}/api/download?{qs}"
        text = urllib.parse.quote(f"Hi! Here are your photos from {booth}: {dl}")
        out.append({**r, "download_url": dl,
                    "wa_link": f"https://wa.me/{r['phone']}?text={text}"})
    return {"pending": out, "count": len(out)}


@app.post("/api/consent/whatsapp/sent")
async def whatsapp_sent(body: dict, _: None = Depends(require_auth)) -> dict:
    """Admin: mark a recipient's photos delivered so they're never queued again."""
    return consent.store.whatsapp_mark_sent(str(body.get("phone", "")),
                                            body.get("photos"))


@app.get("/api/consent/stats")
async def consent_stats(_: None = Depends(require_auth)) -> dict:
    return consent.store.stats()


_email_last: dict[str, float] = {}      # client ip -> last send ts (light rate limit)


@app.post("/api/share/email")
async def share_email(body: dict, request: Request) -> dict:
    ip = request.client.host if request.client else "?"
    if time.time() - _email_last.get(ip, 0) < 15:
        return {"ok": False, "error": "please wait a moment before sending again"}
    files = share.resolve_photos(list(body.get("photos") or []))
    if not files:
        return {"ok": False, "error": "no photos selected"}
    s = config.load()
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None, lambda: share.send_email(str(body.get("to", "")).strip(), files, s))
    if res.get("ok"):
        now = time.time()
        if len(_email_last) > 1000:      # bound the map: drop entries past the 15s window
            for k in [k for k, t in _email_last.items() if now - t > 15]:
                _email_last.pop(k, None)
        _email_last[ip] = now
    return res


@app.post("/api/share/links")
async def share_links(body: dict) -> dict:
    """Public cloud URLs for the selected photos (guest pastes/sends them in WhatsApp)."""
    files = share.resolve_photos(list(body.get("photos") or []))
    if not files:
        return {"ok": False, "error": "no photos selected"}
    s = config.load()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: share.public_links(files, s))


@app.post("/api/test/email")
async def test_email(body: dict, _: None = Depends(require_auth)) -> dict:
    """Admin: send a test email (no attachments) to verify the SMTP settings."""
    s = config.load()
    e = s.share.email
    to = str(body.get("to", "")).strip() or e.smtp_user
    if not share.valid_email(to):
        return {"ok": False, "error": "enter a destination email address"}
    if not (e.smtp_host and e.smtp_user and e.smtp_password):
        return {"ok": False, "error": "email is not configured (SMTP host/user/password)"}
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = f"{s.general.booth_name} — test email"
    msg["From"] = e.from_addr or e.smtp_user
    msg["To"] = to
    msg.set_content("SMTP settings work — the booth can email photos to guests.")

    def _send():
        try:
            if e.use_tls:
                with smtplib.SMTP(e.smtp_host, e.smtp_port, timeout=30) as c:
                    c.starttls()
                    c.login(e.smtp_user, e.smtp_password)
                    c.send_message(msg)
            else:
                with smtplib.SMTP_SSL(e.smtp_host, e.smtp_port, timeout=30) as c:
                    c.login(e.smtp_user, e.smtp_password)
                    c.send_message(msg)
            return {"ok": True, "to": to}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _send)


# ---- gallery --------------------------------------------------------------
@app.get("/api/gallery")
async def gallery() -> list[dict]:
    root = config.captures_dir()
    out = []
    for d in sorted([p for p in root.iterdir() if p.is_dir()],
                    key=lambda p: p.stat().st_mtime, reverse=True):
        imgs = sorted(p.name for p in d.iterdir()
                      if p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.name != "qr.png")
        if imgs:
            out.append({
                "session": d.name,
                "mtime": d.stat().st_mtime,
                "images": [f"/captures/{d.name}/{n}" for n in imgs],
            })
    return out


@app.delete("/api/gallery/{session}")
async def delete_session(session: str, _: None = Depends(require_auth)) -> dict:
    d = _session_dir(session)
    if not d.is_dir():
        raise HTTPException(404, "not found")
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


# ---- destination tests ----------------------------------------------------
# These run rclone/FTP uploads that can block for 30-120s on flaky venue Wi-Fi.
# They MUST go through the executor: called directly in an async handler they
# freeze the whole event loop — kiosk preview, WebSocket state and gesture
# captures all stall — turning one admin "Test" tap into a guest-visible outage.
def _run_upload_test(fname: str, upload) -> dict:
    tmp = config.data_dir() / fname
    tmp.write_text("photobooth upload test")
    try:
        return upload([tmp])
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/test/gdrive")
async def test_gdrive(_: None = Depends(require_auth)) -> dict:
    s = config.load()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _run_upload_test(
        "_gdrive_test.txt", lambda f: uploaders.gdrive_upload(f, s.storage.gdrive)))


@app.post("/api/test/ftp")
async def test_ftp(_: None = Depends(require_auth)) -> dict:
    s = config.load()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _run_upload_test(
        "_ftp_test.txt", lambda f: uploaders.ftp_upload(f, s.storage.ftp)))


@app.post("/api/test/s3")
async def test_s3(_: None = Depends(require_auth)) -> dict:
    s = config.load()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _run_upload_test(
        "_s3_test.txt", lambda f: uploaders.s3_upload(f, s.storage.s3)))


# ---- Google Drive OAuth (configured entirely from the admin panel) --------
_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GDRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_gdrive_states: dict[str, dict] = {}   # state -> {redirect_uri, ts}


def _gdrive_result_page(ok: bool, msg: str) -> str:
    color = "#16a34a" if ok else "#dc2626"
    icon = (f"<svg width='56' height='56' viewBox='0 0 24 24' fill='none' stroke='{color}' "
            f"stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
            + ("<circle cx='12' cy='12' r='10'/><path d='m9 12 2 2 4-4'/>" if ok else
               "<circle cx='12' cy='12' r='10'/><line x1='12' y1='8' x2='12' y2='12'/>"
               "<line x1='12' y1='16' x2='12.01' y2='16'/>") + "</svg>")
    return (f"<!doctype html><meta charset=utf-8><title>Google Drive</title>"
            f"<body style='font-family:system-ui;background:#0f172a;color:#e2e8f0;"
            f"display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            f"<div style='text-align:center;max-width:32rem;padding:2rem'>"
            f"<div>{icon}</div>"
            f"<h2 style='color:{color}'>{'Google Drive connected' if ok else 'Connection failed'}</h2>"
            f"<p style='opacity:.8'>{msg}</p>"
            f"<p style='opacity:.6'>You can close this tab and return to the admin panel.</p>"
            f"</div></body>")


@app.get("/api/gdrive/authorize")
async def gdrive_authorize(request: Request, _: None = Depends(require_auth)) -> dict:
    g = config.load().storage.gdrive
    if not (g.client_id and g.client_secret):
        raise HTTPException(400, "Enter the Google OAuth Client ID and Secret, then Save, before connecting.")
    redirect_uri = str(request.base_url).rstrip("/") + "/api/gdrive/oauth/callback"
    # prune states older than 10 min
    cutoff = time.time() - 600
    for k in [k for k, v in _gdrive_states.items() if v["ts"] < cutoff]:
        _gdrive_states.pop(k, None)
    state = secrets.token_urlsafe(24)
    _gdrive_states[state] = {"redirect_uri": redirect_uri, "ts": time.time()}
    params = urllib.parse.urlencode({
        "client_id": g.client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": _GDRIVE_SCOPE, "access_type": "offline", "prompt": "consent", "state": state})
    return {"url": f"{_GOOGLE_AUTH}?{params}", "redirect_uri": redirect_uri}


@app.get("/api/gdrive/oauth/callback")
async def gdrive_oauth_callback(request: Request) -> HTMLResponse:
    q = request.query_params
    state = q.get("state")
    st = _gdrive_states.pop(state, None) if state else None
    if q.get("error") or not q.get("code") or not st:
        return HTMLResponse(_gdrive_result_page(False, q.get("error") or "authorization was cancelled or expired — try Connect again"))
    g = config.load().storage.gdrive
    data = urllib.parse.urlencode({
        "code": q["code"], "client_id": g.client_id, "client_secret": g.client_secret,
        "redirect_uri": st["redirect_uri"], "grant_type": "authorization_code"}).encode()
    # Token exchange over the venue's internet can hang up to 20s — run it in the
    # executor so it never blocks the event loop (kiosk/preview/gestures) mid-event.
    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(None, lambda: urllib.request.urlopen(
            urllib.request.Request(_GOOGLE_TOKEN, data=data), timeout=20).read())
        tok = json.loads(raw)
    except Exception as e:
        return HTMLResponse(_gdrive_result_page(False, f"token exchange failed: {e}"))
    if "refresh_token" not in tok:
        return HTMLResponse(_gdrive_result_page(
            False, "Google did not return a refresh token. Remove this app under your Google Account → "
                   "Security → Third-party access, then click Connect again."))
    expiry = (datetime.datetime.now(datetime.timezone.utc)
              + datetime.timedelta(seconds=int(tok.get("expires_in", 3600)))).isoformat()
    rclone_token = json.dumps({
        "access_token": tok["access_token"], "token_type": tok.get("token_type", "Bearer"),
        "refresh_token": tok["refresh_token"], "expiry": expiry})
    config.update({"storage": {"gdrive": {"token": rclone_token, "enabled": True}}})
    return HTMLResponse(_gdrive_result_page(True, "The booth can now upload photos to your Google Drive."))


# ---- background sync ------------------------------------------------------
@app.get("/api/sync/status")
async def sync_status() -> dict:
    return sync_worker.status()


@app.post("/api/sync/retry")
async def sync_retry(_: None = Depends(require_auth)) -> dict:
    sync_worker.retry_now()
    return {"ok": True}


# ---- printing -------------------------------------------------------------
@app.get("/api/print/printers")
async def print_printers(_: None = Depends(require_auth)) -> dict:
    ok, detail = printing.available()
    return {"available": ok, "detail": detail, "printers": printing.printers(),
            "queue": printing.queue()}


@app.post("/api/print")
async def print_now(body: dict, _: None = Depends(require_auth)) -> dict:
    """Print the last session, or a specific session folder. Used for the admin Test
    print and a manual reprint."""
    s = config.load()
    session = body.get("session")
    if session:
        d = _session_dir(str(session))
        files = sorted(str(p) for p in d.iterdir()
                       if p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.name != "qr.png") \
            if d.is_dir() else []
    else:
        files = service.last_finals
    if not files:
        return {"ok": False, "error": "nothing to print yet"}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: printing.print_files(
        files, s.printing.printer, int(body.get("copies") or s.printing.copies),
        s.printing.media, s.printing.fit_to_page))


# ---- network / Wi-Fi / hotspot --------------------------------------------
@app.get("/api/network/status")
async def network_status() -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, wifi.status)


@app.get("/api/wifi/scan")
async def wifi_scan(_: None = Depends(require_auth)) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, wifi.scan)


@app.post("/api/wifi/connect")
async def wifi_connect(body: dict, _: None = Depends(require_auth)) -> dict:
    ssid = str(body.get("ssid", "")).strip()
    if not ssid:
        raise HTTPException(400, "ssid required")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: wifi.connect(ssid, str(body.get("password", ""))))


@app.post("/api/wifi/forget")
async def wifi_forget(body: dict, _: None = Depends(require_auth)) -> dict:
    ssid = str(body.get("ssid", "")).strip()
    if not ssid:
        raise HTTPException(400, "ssid required")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: wifi.forget(ssid))


@app.post("/api/hotspot")
async def hotspot_control(body: dict, _: None = Depends(require_auth)) -> dict:
    """Bring the guest hotspot up/down. Uses the saved network settings (SSID/password/
    band) and always runs on the spare radio — never the management interface."""
    action = body.get("action")
    s = config.load()
    loop = asyncio.get_running_loop()
    if action == "up":
        n = s.network
        res = await loop.run_in_executor(None, lambda: wifi.hotspot_up(
            n.hotspot_ssid, n.hotspot_password, band=n.hotspot_band, hidden=n.hotspot_hidden))
        if res.get("ok"):
            config.update({"network": {"hotspot_enabled": True}})
        return res
    if action == "down":
        res = await loop.run_in_executor(None, wifi.hotspot_down)
        config.update({"network": {"hotspot_enabled": False}})
        return res
    raise HTTPException(400, "action must be 'up' or 'down'")


# ---- websocket (events) ---------------------------------------------------
@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    q = bus.subscribe()
    try:
        await websocket.send_json(bus.last_state)  # current state for new client
        while True:
            event = await q.get()
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(q)
