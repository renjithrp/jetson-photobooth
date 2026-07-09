"""FastAPI application: serves the kiosk + admin UIs and the booth API."""
from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import logging
from contextlib import asynccontextmanager
import os
import shutil
import socket
import subprocess
import threading
import urllib.request
from pathlib import Path

import qrcode

from fastapi import (Depends, FastAPI, File, HTTPException, Request, Response,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, face_index, faces, gestures, liveview, printing, uploaders, wifi
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


def _qr_data_uri(text: str) -> str:
    """Render `text` as a QR PNG and return it as a data: URI (for inline <img>)."""
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


@app.post("/api/login")
async def login(body: dict, response: Response) -> dict:
    if not auth.check_pin(str(body.get("pin", ""))):
        raise HTTPException(401, "invalid PIN")
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
    safe = session.replace("/", "").replace("..", "")
    sess = config.captures_dir() / safe
    if not sess.is_dir():
        raise HTTPException(404, "session not found")
    imgs = sorted(p.name for p in sess.iterdir()
                  if p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.name != "qr.png")
    items = "".join(
        f'<a href="/captures/{safe}/{n}" download><img src="/captures/{safe}/{n}"></a>'
        for n in imgs)
    html = f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Your Photos</title>
<style>body{{font-family:system-ui;background:#111;color:#eee;text-align:center;margin:0;padding:16px}}
img{{max-width:100%;border-radius:12px;margin:8px 0;box-shadow:0 6px 24px #0008}}
h1{{font-weight:600}} a{{display:block}}</style>
<h1>📸 Your Photos</h1><p>Tap an image to download.</p>{items}"""
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
@app.post("/api/capture")
async def manual_capture() -> dict:
    if service.busy:
        return {"ok": False, "reason": "busy"}
    asyncio.create_task(service.run_session("manual"))
    return {"ok": True}


@app.post("/api/focus")
async def manual_focus() -> dict:
    """Trigger autofocus via the camera daemon (for the mobile control page)."""
    s = config.load()
    if not (s.camera.backend == "sony" and s.preview.source == "sony_http"):
        return {"ok": False, "reason": "autofocus needs the Sony camera daemon"}
    base = (s.preview.sony_http_url or "http://127.0.0.1:8080/").rstrip("/")
    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(
            None, lambda: urllib.request.urlopen(base + "/focus", timeout=8).read())
        return json.loads(raw.decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}


_SERVICES = {"photobooth", "photobooth-camera", "photobooth-kiosk"}
_ACTIONS = {"start", "stop", "restart"}


@app.post("/api/system/service")
async def service_control(body: dict, _: None = Depends(require_auth)) -> dict:
    """Start/stop/restart booth services from the admin UI (app runs as root)."""
    svc, action = body.get("service"), body.get("action")
    if svc not in _SERVICES or action not in _ACTIONS:
        raise HTTPException(400, "invalid service or action")
    unit = f"{svc}.service"
    # Detached so the HTTP response returns immediately (systemctl restart can block
    # several seconds, and restarting the backend itself would kill this request).
    delay = "1" if svc == "photobooth" else "0"
    subprocess.Popen(["sh", "-c", f"sleep {delay}; systemctl {action} {unit}"])
    return {"ok": True, "detached": True, "service": svc, "action": action}


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
    tmp = config.data_dir() / "_find_selfie.jpg"
    tmp.write_bytes(await selfie.read())
    loop = asyncio.get_running_loop()
    try:
        embs = await loop.run_in_executor(None, lambda: eng.embed_image(tmp))
    finally:
        tmp.unlink(missing_ok=True)
    if not embs:
        return {"matched": False, "error": "no face detected — try again"}
    return face_index.index.match(embs[0], s.faces.match_threshold)


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
    safe = session.replace("/", "").replace("..", "")
    d = config.captures_dir() / safe
    if not d.is_dir():
        raise HTTPException(404, "not found")
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


# ---- destination tests ----------------------------------------------------
@app.post("/api/test/gdrive")
async def test_gdrive(_: None = Depends(require_auth)) -> dict:
    s = config.load()
    tmp = config.data_dir() / "_gdrive_test.txt"
    tmp.write_text("photobooth gdrive test")
    res = uploaders.gdrive_upload([tmp], s.storage.gdrive)
    tmp.unlink(missing_ok=True)
    return res


@app.post("/api/test/ftp")
async def test_ftp(_: None = Depends(require_auth)) -> dict:
    s = config.load()
    tmp = config.data_dir() / "_ftp_test.txt"
    tmp.write_text("photobooth ftp test")
    res = uploaders.ftp_upload([tmp], s.storage.ftp)
    tmp.unlink(missing_ok=True)
    return res


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
        safe = str(session).replace("/", "").replace("..", "")
        d = config.captures_dir() / safe
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
