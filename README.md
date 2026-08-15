# AI Photo Booth

A kiosk photo booth for the **NVIDIA Jetson Orin Nano** with a **Sony A7R IV**
(Camera Remote SDK), controlled entirely from a web admin dashboard.

## Features

- **Kiosk UI** (fullscreen Chromium): live preview, countdown, multi-shot, review with QR.
- **Web admin dashboard** (`/admin`, PIN-gated) for *all* settings — no SSH needed.
- **Triggers**: hand **gesture** (MediaPipe, isolated worker) or **GPIO/Arduino** button.
- **Timer**: countdown, number of shots, interval, review time, attract-screen idle.
- **Live preview**: mock / USB webcam / Sony CrSDK live-view HTTP.
- **Guest Wi-Fi + sharing**: captive hotspot, on-screen **QR code**, per-guest select /
  ZIP / WhatsApp / email, **Google Drive** (OAuth), **S3**, **FTP/FTPS**.
- **Overlay frames + logo**, **multi-shot collages** (strip / 2×2).
- **Face grouping** on the **Jetson GPU** (InsightFace, CUDA) — "find my photos" by selfie.
- **AI background effects** hook (rembg) — wired, currently disabled.
- **Gallery** + attract screen; **auto-start on boot**; **admin URL shown in the kiosk corner**.

## Architecture

```
Chromium kiosk ─┐
                ├─► FastAPI (backend/) ─► CameraBackend (mock | sony CrSDK | webcam)
Admin browser ──┘        │                 LiveView (MJPEG)
                         │                 Triggers (gesture worker / GPIO)
                         │                 Uploaders (Google Drive / S3 / FTP)
                         └─ WebSocket /ws drives the kiosk state machine
```

- Camera default is **`mock`** so the app runs with no hardware. Switch to **`sony`** in
  the admin once the CrSDK capture/transfer is confirmed (see `native/`).
- Settings live in `data/settings.json` (created on first run, editable via admin).
- Photos are saved under `data/captures/session_*/` and served at `/captures/...`.

## Run locally (dev, mock camera)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn backend.main:app --reload --port 8000
# Kiosk:  http://localhost:8000/        Admin: http://localhost:8000/admin  (PIN 1234)
# Trigger a capture from the admin "Test capture" button.
```

## Deploy to the Jetson

The booth runs at `/opt/photobooth`, owned by the `pb` user (passwordless sudo).

```bash
# first time (installs deps, services, autostart):
rsync -az ./ pb@192.168.86.30:/opt/photobooth/
ssh pb@192.168.86.30 'bash /opt/photobooth/deploy/setup-jetson.sh'

# subsequent updates (syncs, restarts the Python services):
./deploy/deploy.sh pb@192.168.86.30
./deploy/deploy.sh pb@192.168.86.30 -n        # dry run
./deploy/deploy.sh pb@192.168.86.30 --deps    # also refresh venv from requirements.txt
```

`deploy.sh` excludes everything generated on the Jetson — `venv/`, `gesture-venv/`,
`data/`, `models/`, `wheels/`, `certs/` — so `--delete` never touches them.

Services (all enabled at boot): `photobooth` (FastAPI backend), `photobooth-camera`
(Sony CrSDK daemon), `photobooth-gesture` (isolated MediaPipe worker),
`photobooth-captive` (guest portal on `:80`). The Chromium kiosk starts from
`~/.config/autostart/photobooth-kiosk.desktop` in the `pb` GNOME session — not a
systemd unit (see `deploy/start-kiosk.sh`).

## Sony camera (real capture)

The CrSDK helper lives in `native/boothCapture.cpp`. Copy Sony's SDK to `/opt/CrSDK`,
then build on the Jetson:

```bash
ssh pb@192.168.86.30 'CRSDK=/opt/CrSDK bash /opt/photobooth/native/build.sh'
```

Then set **Camera → backend = sony** in the admin. The A7R IV must have
**MENU → Network → PC Remote Function → Still Img. Save Dest. = PC Only**.
Booth uses the **small** transfer size by default (full-res can drop the USB link).

## Cloud uploads

Google Drive, S3 and FTP are configured entirely from **Admin → Sharing/Storage**.
Drive uses an in-browser OAuth flow (paste a Google OAuth client ID/secret, then
authorise); rclone is the transport underneath and is installed by `setup-jetson.sh`.
Uploads run through a durable sync queue, so they survive restarts and outages.

## Guest Wi-Fi (captive portal)

A USB Wi-Fi dongle hosts the guest AP (`192.168.50.1/24`) while the onboard adapter
keeps the LAN/internet uplink — the two are never mixed. Default mode is
**pass-through**: guests keep their internet and reach photos by scanning the
on-screen QR. Set up with `deploy/setup-captive.sh`; SSID/password are set in admin.

## Security & operations

- **Admin auth is server-side.** Log in with the PIN (default `1234` — change it in
  Admin → General) to get a signed, HttpOnly session cookie. Config, system control,
  gallery deletion and destination tests all require it. **Secrets (admin PIN, FTP
  password, cloud keys) are never returned by the API** — fields show blank and
  "leave blank to keep".
- **Open (no auth) on the LAN, by design:** the kiosk page, live preview, the guest
  capture page (`/control`), and capture/focus — so guests can use the booth.
- **Health:** `GET /api/system/info` reports camera + live-view-daemon status, disk
  free, and version; the admin dashboard shows these live.
- **Service control from the admin:** restart the app / camera / kiosk, or stop/start
  the kiosk, from the System control card (runs `systemctl` detached).
- **Logs:** `journalctl -u photobooth -f` (backend), `-u photobooth-camera`
  (CrSDK daemon), `-u photobooth-gesture` (gesture worker), `-u photobooth-captive`.

## HTTPS / TLS

HTTPS protects the admin PIN/session cookie over the network. Enable it on the Jetson:

```bash
ssh pb@192.168.86.30 'bash /opt/photobooth/deploy/gen-cert.sh && sudo systemctl restart photobooth.service'
```

`gen-cert.sh` writes a self-signed cert (SANs for the booth IP/hostname/localhost) to
`certs/`. The launcher (`run-backend.sh`) auto-enables TLS when those files exist and
the kiosk follows the scheme automatically (accepting the self-signed cert locally).
Admin/remote: `https://<booth-ip>:8000/admin` — browsers show a one-time trust warning
for self-signed certs (expected on a LAN). To remove the warning, install the cert on
your devices or use a real cert (e.g. Let's Encrypt behind a domain). To disable TLS,
delete `certs/` and restart. WebSockets use `wss://` automatically over HTTPS.

## Face grouping (group photos by person, on the GPU)

Detection and embedding both run on the **Jetson GPU** via InsightFace `buffalo_l`
(SCRFD + ArcFace) on onnxruntime-gpu/CUDA. Captured faces are clustered into "people";
the admin shows a People view and guests can **"find my photos"** with a selfie.

`setup-jetson.sh` installs this — including the sm_87 `onnxruntime-gpu` wheel if one is
present in `wheels/` (otherwise it falls back to CPU, which is much slower). Then enable
it in **Admin → Face grouping**.

`GET /api/faces/status` reports readiness; embeddings/clusters live in `data/faces.json`.
Tune **match threshold** in admin (higher = stricter).

> **Legacy:** `deploy/install-faces.sh` and `native/convert_arcface_to_rknn.py` target the
> original Orange Pi 5B / RK3588 NPU (RKNN) build. They are **not** used on the Jetson.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q          # API auth/masking/capture + gesture-classification unit tests
```

The suite uses a temp data dir and the mock camera, so it runs anywhere (no hardware).

## Status / roadmap

- ✅ Full app scaffold, admin, kiosk, uploaders, triggers, systemd, deploy.
- ✅ Face grouping on CUDA; gesture trigger via isolated MediaPipe worker.
- ✅ Guest captive Wi-Fi + sharing (select / ZIP / WhatsApp / email).
- ⏳ Sony full-frame USB transfer stability (use small size / verify cable/port).
- ⏳ Gaze correction — measure-only scaffold in place; redirection model TODO.
- ⏳ AI background segmentation (rembg hook in `processing.apply_ai`) — currently off.
