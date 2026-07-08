# AI Photo Booth

A kiosk photo booth for the **Orange Pi 5B** with a **Sony A7R IV** (Camera Remote SDK),
controlled entirely from a web admin dashboard.

## Features

- **Kiosk UI** (fullscreen Chromium): live preview, countdown, multi-shot, review with QR.
- **Web admin dashboard** (`/admin`, PIN-gated) for *all* settings — no SSH needed.
- **Triggers**: hand **gesture** (MediaPipe) or **GPIO** button (or both).
- **Timer**: countdown, number of shots, interval, review time, attract-screen idle.
- **Live preview**: mock / USB webcam / Sony CrSDK live-view HTTP.
- **Sharing**: on-screen **QR code**, **Google Drive** (rclone), **FTP/FTPS**.
- **Overlay frames + logo**, **multi-shot collages** (strip / 2×2).
- **AI background effects** hook for the RK3588 **NPU** (RKNN) — wired, model pluggable.
- **Gallery** + attract screen; **auto-start on boot**; **admin URL shown in the kiosk corner**.

## Architecture

```
Chromium kiosk ─┐
                ├─► FastAPI (backend/) ─► CameraBackend (mock | sony CrSDK | webcam)
Admin browser ──┘        │                 LiveView (MJPEG)
                         │                 Triggers (GPIO / gesture)
                         │                 Uploaders (Google Drive / FTP)
                         └─ WebSocket /ws drives the kiosk state machine
```

- Camera default is **`mock`** so the app runs with no hardware. Switch to **`sony`** in
  the admin once the CrSDK capture/transfer is confirmed (see `native/`).
- Settings live in `data/settings.json` (created on first run, editable via admin).
- Photos are saved under `data/captures/session_*/` and served at `/captures/...`.

## Run locally (dev, mock camera)

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn backend.main:app --reload --port 8000
# Kiosk:  http://localhost:8000/        Admin: http://localhost:8000/admin  (PIN 1234)
# Trigger a capture from the admin "Test capture" button.
```

## Deploy to the Pi

```bash
# first time (installs deps, services, autostart):
rsync -az ./ root@192.168.86.105:/opt/photobooth/
ssh root@192.168.86.105 'bash /opt/photobooth/deploy/setup-pi.sh'

# subsequent updates:
./deploy/deploy.sh root@192.168.86.105
```

Backend autostarts via `photobooth.service`; the Chromium kiosk via
`photobooth-kiosk.service` (adjust the desktop user in that unit if not `orangepi`).

## Sony camera (real capture)

The CrSDK helper lives in `native/boothCapture.cpp`. Build it on the Pi:

```bash
ssh root@192.168.86.105 'CRSDK=/root/CrSDK bash /opt/photobooth/native/build.sh'
```

Then set **Camera → backend = sony** in the admin. The A7R IV must have
**MENU → Network → PC Remote Function → Still Img. Save Dest. = PC Only**.
Booth uses the **small** transfer size by default (full-res can drop the USB link).

## Google Drive setup (rclone)

```bash
ssh root@192.168.86.105 'rclone config'   # create a remote named e.g. "gdrive"
```
Then in admin: Storage → Google Drive → enable, set the remote name + folder.

## Security & operations

- **Admin auth is server-side.** Log in with the PIN (default `1234` — change it in
  Admin → General) to get a signed, HttpOnly session cookie. Config, system control,
  gallery deletion and destination tests all require it. **Secrets (admin PIN, FTP
  password) are never returned by the API** — fields show blank and "leave blank to keep".
- **Open (no auth) on the LAN, by design:** the kiosk page, live preview, the guest
  capture page (`/control`), and capture/focus — so guests can use the booth.
- **Health:** `GET /api/system/info` reports camera + live-view-daemon status, disk
  free, and version; the admin dashboard shows these live.
- **Service control from the admin:** restart the app / camera / kiosk, or stop/start
  the kiosk, from the System control card (runs `systemctl` detached).
- **Logs:** `journalctl -u photobooth -f` (backend), `-u photobooth-liveview`
  (camera daemon), `-u photobooth-kiosk` (Chromium).
## HTTPS / TLS

HTTPS protects the admin PIN/session cookie over the network. Enable it on the Pi:

```bash
ssh root@192.168.86.105 'bash /opt/photobooth/deploy/gen-cert.sh && systemctl restart photobooth.service && systemctl restart photobooth-kiosk.service'
```

`gen-cert.sh` writes a self-signed cert (SANs for the Pi IP/hostname/localhost) to
`certs/`. The launcher (`run-backend.sh`) auto-enables TLS when those files exist and
the kiosk follows the scheme automatically (accepting the self-signed cert locally).
Admin/remote: `https://<pi-ip>:8000/admin` — browsers show a one-time trust warning
for self-signed certs (expected on a LAN). To remove the warning, install the cert on
your devices or use a real cert (e.g. Let's Encrypt behind a domain). To disable TLS,
delete `certs/` and restart. WebSockets use `wss://` automatically over HTTPS.

## Face grouping (group photos by person, on the NPU)

Detection runs on CPU (MediaPipe); the **ArcFace embedding runs on the RK3588 NPU**
(RKNN). Captured faces are clustered into "people"; the admin shows a People view and
guests can **"find my photos"** with a selfie on `/control`.

One-time enablement:

```bash
# 1) On the Pi: install detection + NPU runtime
ssh root@192.168.86.105 'bash /opt/photobooth/deploy/install-faces.sh'

# 2) On an x86 Linux host: convert an ArcFace ONNX (112x112) to RKNN
pip install rknn-toolkit2
python native/convert_arcface_to_rknn.py w600k_mbf.onnx arcface.rknn
scp arcface.rknn root@192.168.86.105:/opt/photobooth/models/arcface.rknn

# 3) Admin → Face grouping → Enabled (model path defaults to that file), Save.
```

`GET /api/faces/status` reports readiness; embeddings/clusters live in `data/faces.json`.
Tune **match threshold** in admin (higher = stricter). Get an ArcFace ONNX from
InsightFace (`w600k_mbf` = small/fast, `w600k_r50` = more accurate).

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q          # API auth/masking/capture + gesture-classification unit tests
```

The suite uses a temp data dir and the mock camera, so it runs anywhere (no hardware).

## Status / roadmap

- ✅ Full app scaffold, admin, kiosk, uploaders, triggers, systemd, deploy.
- ⏳ Sony full-frame USB transfer stability (use small size / verify cable/port).
- ⏳ MediaPipe gesture on aarch64 (may need a custom wheel); GPIO is ready.
- ⏳ RKNN AI background model (hook in `processing.apply_ai`).
