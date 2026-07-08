# PhotoBooth Pro — Jetson Orin Nano — Technical Specification

**Status:** Draft v1 · **Date:** 2026-07-07 · **Target device:** `pb@192.168.86.250`
**Reference system:** Orange Pi 5B booth at `root@192.168.86.105` (feature parity + upgrades)

---

## 1. Goal

Build a **stable, enterprise-grade AI photo booth** on the NVIDIA Jetson Orin Nano that
matches every feature of the existing Orange Pi booth and adds GPU-accelerated AI, easier
setup, a friendlier UI, and robust offline + cloud operation. It must work **fully offline**
(guest hotspot, on-device face recognition, local download) **and** online (background sync to
Google Drive / FTP), and be operable end-to-end from a **web admin panel** with no SSH.

---

## 2. Hardware & Platform

| | Orange Pi 5B (reference) | **Jetson Orin Nano (target)** |
|---|---|---|
| SoC | RK3588 (RKNN NPU) | Orin (Ampere GPU + CUDA/TensorRT) |
| OS | Ubuntu (rockchip 6.1) | **Ubuntu 24.04, L4T r39 / JetPack 7.2, kernel 6.8-tegra** |
| Python | 3.x | **3.12** |
| AI runtime | RKNN (NPU) | **CUDA + TensorRT + onnxruntime-gpu** (installing via `nvidia-jetpack`) |
| Camera | Sony A7R IV via CrSDK (ARMv8) | Same CrSDK v2.02 ARMv8 build (aarch64 → runs on Jetson) |
| Trigger | GPIO button + gesture | **USB Arduino Nano (serial)** + gesture |
| Wi-Fi | onboard + USB dongle | **M.2 PCIe Realtek `wlP1p1s0` (internet)** + **USB TP-Link AC (guest AP)** |
| Storage | SD/eMMC | **1.8 TB NVMe** |

**Connected now:** M.2 Wi-Fi (management/internet), USB TP-Link dongle (driver not yet loaded),
Bluetooth, USB hubs. **To connect for validation:** Sony camera (USB), Arduino Nano (USB serial).

**Decisions locked (2026-07-07):**
- Kiosk on a **non-touch monitor** — UI must be fully operable via gesture / Arduino button (no touch reliance).
- AI runs on **GPU via onnxruntime + InsightFace** (RetinaFace detect + ArcFace embed); JetPack/CUDA installed.
- Validate against **real Sony camera, Arduino, and TP-Link hotspot**.
- Priority extra features: **AI background replace, auto-capture (smile/pose), AI filters + beautify, printing.**

---

## 3. Architecture

```
                      ┌─────────────────────────────────────────────┐
   Monitor (kiosk) ◄──┤  Chromium kiosk  (fullscreen, gesture-driven)│
                      └───────────────┬─────────────────────────────┘
   Admin browser ─────┐               │ WebSocket /ws (state machine)
   Guest phone   ─────┤               ▼
   (hotspot)          │        FastAPI backend  (systemd: photobooth)
                      └──►  ┌──────────────────────────────────────────────┐
                           │ CaptureService · TriggerManager · SyncWorker  │
                           │ FaceEngine(GPU) · AI effects(GPU) · Uploaders │
                           │ WifiManager · Watchdog · Config · Auth        │
                           └───────┬───────────────┬──────────────┬────────┘
                                   │ HTTP :8080    │ serial        │ nmcli/rclone
                           ┌───────▼──────┐  ┌─────▼──────┐   ┌────▼─────────┐
                           │ cameraDaemon │  │ Arduino    │   │ NetworkMgr / │
                           │ (Sony CrSDK) │  │ Nano (USB) │   │ TP-Link AP   │
                           │ liveview+cap │  └────────────┘   └──────────────┘
                           └──────────────┘
```

- **Single camera session:** one C++ `cameraDaemon` holds the Sony PC-Remote session and
  serves live-view MJPEG (`:8080/`) **and** capture (`/capture`) + autofocus (`/focus`). The
  backend's `sony_hub` is the single MJPEG consumer; it buffers the latest frame for browsers
  (same-origin, no connection leak) and runs gesture detection on those frames.
- **State machine over WebSocket:** `idle → countdown → capturing → processing → review → idle`,
  broadcast to kiosk + control pages.
- **Everything configurable from `/admin`** (PIN-gated); settings persist to `data/settings.json`
  (pydantic-validated, deep-merged with defaults so upgrades add fields automatically).

---

## 4. Feature set

### 4.1 Ported from Orange Pi (kept, hardened)
- Kiosk UI: live preview, attract screen, countdown, multi-shot, review + QR.
- Admin dashboard: all settings, live health, service control, gallery, destination tests.
- Guest "find-my-photos" (selfie) + mobile control page.
- Triggers with face-zone gating + cooldown + hold-to-start.
- Overlays (frame PNG + logo), collages (strip / 2×2).
- Sharing: on-screen QR, Google Drive (rclone), FTP/FTPS.
- Guest **hotspot** for offline download; TLS; watchdog + plug-and-play camera reconnect.
- systemd services, auto-start on boot, atomic settings, secret masking.

### 4.2 Upgraded on Jetson
- **Face recognition on GPU** — RetinaFace + ArcFace (InsightFace/onnxruntime-gpu) replaces
  MediaPipe+RKNN. Better detection at booth distance, faster, higher accuracy clustering.
- **USB Arduino Nano trigger** — serial protocol replaces GPIO; hot-pluggable, debounced,
  supports multiple button events (capture / retake / print).
- **Background sync worker** — durable offline queue with retry/backoff; uploads resume when
  internet returns (replaces inline, blocking uploads).
- **Two-radio networking** — guest AP on the USB TP-Link + internet on the M.2 simultaneously;
  full Wi-Fi management (scan/join/forget) from the admin.

### 4.3 New (priority)
- **AI background remove/replace** — GPU portrait segmentation (e.g. MODNet / RobustVideoMatting /
  BiSeNet); per-event virtual backdrops; optional live-preview matting.
- **Auto-capture** — fire on detected **smile** / stable pose / eyes-open; hands-free mode.
- **AI filters + beautify** — style/tone filters, skin smoothing, relight; applied per-shot or collage.
- **Printing** — CUPS-based direct print to USB/network photo printer, with print button + copies.

### 4.4 New (roadmap / nice-to-have)
- Boomerang / GIF / short video messages.
- Analytics (sessions, uptime, popular gestures), event mode, config export/import, OTA update,
  first-run setup wizard, multi-language.

---

## 5. Component design

### 5.1 Native Sony layer (`native/`)
- `cameraDaemon.cpp` — unified live-view + capture daemon (already Jetson-aware via
  `USE_EXPERIMENTAL_FS`). Built against CrSDK at `/opt/CrSDK` on the Jetson.
- Build with `g++ -std=c++17 … -lCr_Core`; ship `libCr_Core.so` + `CrAdapter/` alongside via rpath `$ORIGIN`.
- Tegra specifics: USB **autosuspend disabled** (udev rule + kernel arg), keep-alive S1 pulse,
  systemd `Restart=always` for seamless reconnect.

### 5.2 Backend (`backend/`, FastAPI)
Modules (ported names retained where possible): `main` (API+UI+WS), `config`, `models`
(pydantic settings), `camera`, `sony_hub`, `liveview`, `capture_service`, `triggers`,
`gestures`, `faces` (GPU engine), `face_index` (engine-agnostic clustering — reused as-is),
`uploaders` + new `sync` (background worker), `processing` (overlay/collage + AI effects),
`ai_effects` (GPU segmentation/filters — new), `autocapture` (new), `wifi` (nmcli manager — new),
`printing` (CUPS — new), `auth`, `events`, `watchdog`.

### 5.3 Triggers
- **ArduinoTrigger** (new): reads newline-delimited events from `/dev/ttyACM*`/`ttyUSB*`
  (auto-detect by USB VID/PID 2341/1a86/0403). Protocol: device sends `TRIG`, `RETAKE`,
  `PRINT`; host may send `LED:on/off`, `READY`. Debounce + reconnect on unplug.
- **GestureTrigger / SonyFrameHub gesture**: on GPU (or CPU MediaPipe fallback), hold-to-start,
  face-zone gating, cooldown.

### 5.4 AI stack (GPU)
- Face: `insightface`/onnxruntime-gpu, models cached under `models/`. `embed_image()` returns
  L2-normalized 512-d vectors; `face_index` clusters online by cosine similarity.
- Effects: segmentation model for background matting; filter LUTs / lightweight GPU ops.
- Auto-capture: smile/eyes via face attributes or a small classifier on the live frames.
- All AI **degrades gracefully** — if CUDA/model missing, features report unavailable and the
  booth keeps working.

### 5.5 Networking & offline (`wifi` + deploy)
- **Internet (STA):** M.2 `wlP1p1s0` managed by NetworkManager; admin can scan/join/forget.
- **Guest AP:** USB TP-Link (driver `rtl88x2bu`/`8821au` as needed) in AP mode via
  `nmcli … ipv4.method shared` → DHCP + NAT for guests; SSID/password set in admin.
- **Offline path:** guests join the hotspot, scan the kiosk join-QR, open `/booth`, find their
  photos by selfie (on-device), download locally — **no internet required**.
- **Online path:** background sync pushes finished photos to Google Drive/FTP; QR can point at a
  public URL when configured.

### 5.6 Storage & data
- Captures under `data/captures/session_YYYYmmdd_HHMMSS/` (NVMe). Local retention policy
  (keep N sessions). `faces.json` index. `settings.json` config. `certs/` TLS.
- Sync queue persisted (survives reboot); per-file upload state tracked.

### 5.7 Security
- Admin PIN → signed HttpOnly cookie; secrets never returned by API (blank = keep).
- Public-on-LAN by design for guest/kiosk/capture; config + system control require auth.
- Optional self-signed TLS (cert includes LAN IP + hotspot IP); WS upgrades to `wss` automatically.

### 5.8 Deployment & ops
- Dev repo here → `rsync` to `pb@192.168.86.250:/opt/photobooth` → `setup-jetson.sh` (installs
  deps, venv, services, autostart, udev, kiosk).
- systemd units: `photobooth` (backend), `photobooth-camera` (Sony daemon), `photobooth-kiosk`
  (Chromium), `photobooth-sync` (or in-process worker). Health at `GET /api/system/info`.
- Logs via journald; admin can restart services and view status live.

---

## 6. Phased roadmap

| Phase | Deliverable | Validates |
|---|---|---|
| **0** | Repo scaffold, base port running with mock camera under systemd; JetPack installing; SPEC | App boots, admin/kiosk reachable |
| **1** | Sony CrSDK on Jetson: live view + capture + focus, autosuspend/keepalive/watchdog | Real camera |
| **2** | USB Arduino trigger + AI gesture on py3.12 aarch64 | Real Arduino, gesture |
| **3** | GPU face recognition + offline find-my-photos | GPU, InsightFace |
| **4** | TP-Link hotspot + Wi-Fi admin + offline download | Real hotspot, guest phone |
| **5** | Background sync to Google Drive + FTP (offline queue) | Cloud, retry |
| **6** | AI bg-replace, auto-capture, filters/beautify, printing; enterprise polish | Priority AI + printer |

---

## 7. Risks & open items
- **MediaPipe on Python 3.12 / aarch64** may lack a wheel → fall back to a GPU/ONNX hand-gesture
  model or build from source. Validate in Phase 2.
- **TP-Link driver** for `2357:0138` (Archer T3U/T2U) on kernel 6.8-tegra may need an out-of-tree
  DKMS build (`rtl88x2bu` / `8821au`). Validate in Phase 4.
- **Single M.2 radio during setup** — until the USB dongle driver loads, hotspot + internet share
  one radio; two-radio is the supported target.
- Sony **full-res USB transfer** can drop the link → default to small transfer size (as reference).
- CrSDK ARMv8 build assumed ABI-compatible on Tegra 6.8 — confirm at Phase 1 build/run.

---

## 8. Repo layout (this repo → deploys to `/opt/photobooth`)
```
backend/      FastAPI app + modules            native/     Sony CrSDK C++ (+ build.sh)
frontend/     kiosk · admin · guest · control  deploy/     setup-jetson.sh, services, udev, hotspot
models/       cached AI models (gitignored)    tests/      pytest (mock camera, no hardware)
data/         runtime (gitignored)             SPEC.md     this document
```
