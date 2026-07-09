# PhotoBooth — Current Deployment Specs & State

Snapshot as of **2026-07-08**. Live booth: `pb@192.168.86.250` (LAN), app at `/opt/photobooth`.

## Hardware / Platform
| | |
|---|---|
| Board | NVIDIA Jetson Orin Nano Developer Kit (`tegra234`) |
| CPU | 6 cores, pinned @ 1510 MHz (`jetson_clocks`) |
| RAM | 7.3 GiB usable |
| Swap | 19 GiB — zram 3.7 G (prio 100) + `/swapfile` 16 G (prio -2) |
| Power mode | 15 W (max on this image; no 25 W MAXN available) |
| L4T | R39 (release) 2.0 · kernel `6.8.12-1021-tegra` |
| Storage | 1.8 TB NVMe, 6% used |
| Display | 4K panel driven at **1920x1080@60** (Chromium snap has no GPU access → software render; 1080p keeps it ~0.6 core) |

## Software
| | |
|---|---|
| Backend | Python 3.12.3, FastAPI, `/opt/photobooth/venv` |
| AI runtime | onnxruntime-gpu (CUDA EP; TensorRT EP present but dropped from provider lists) |
| Face grouping | InsightFace `buffalo_l` (SCRFD + ArcFace) on CUDA |
| Segmentation | rembg (AI background — currently disabled) |
| Gesture | MediaPipe in isolated `/opt/photobooth/gesture-venv` (numpy<2, can't share main venv) |
| Uploads | rclone v1.60.1 (Google Drive / S3 / FTP) |
| Kiosk | Chromium snap, `--kiosk`, forced fullscreen via wmctrl |

## Services (all active + enabled)
`photobooth` · `photobooth-camera` · `photobooth-captive` · `photobooth-gesture` · `jetson-clocks` · `zramswap`
(kiosk starts via `~/.config/autostart` in the pb GNOME session)

## Camera / Live view
- Sony A7R IV via CrSDK daemon on `:8080`; backend hub buffers frames (multi-client).
- Live-view source `sony_http`, ~1024×680 MJPEG, kiosk stream throttled to **10 fps**, selfie-mirrored.

## Network
| Role | Interface | Details |
|---|---|---|
| Management / SSH / internet | `wlP1p1s0` (onboard) | LAN "Virus-5G", `192.168.86.250/24` — **never used for the AP** |
| Guest hotspot (AP) | `wlx782051871b2c` (USB dongle) | SSID **PhotoBooth** / pass **booth1234**, `192.168.50.1/24`, 2.4 GHz, visible |
| Captive portal | — | `:80` reverse-proxy, **pass-through mode** (guests keep internet; reach photos by scanning the on-screen QR → opens in real browser). `share.base_url = http://192.168.50.1` |

## Feature state
- **Trigger**: gesture, `open_palm` (detects from distance: ≥3 fingers, lowered thresholds). Arduino button wired.
- **Face grouping**: ON (CUDA). **AI background**: OFF. **Gaze correction**: OFF (measure-only scaffold).
- **Printing** (CUPS): OFF.
- **Cloud uploads**: gdrive/S3/FTP all OFF (configured from admin → Sharing; Drive uses in-browser OAuth).

## This session's key changes (repo `master`, local-only — no git remote)
Kiosk fullscreen (wmctrl) · gesture worker + distance tuning · gaze scaffold · OOM fix (zram+swap) · jetson_clocks · 1080p render + drop TensorRT EP · fix intermittent numpy/cv2 face-grouping race · kiosk hide-preview-during-processing · Google Drive (OAuth) + S3 uploads · captive Wi-Fi (AP IP fix, http/https auto-detect, pass-through default).

## Admin
- Admin UI: `http://192.168.86.250:8000/admin` (PIN-gated).
- Deploy method: `scp` files to `/opt/photobooth` + `sudo systemctl restart <svc>` (no git on the Jetson).
