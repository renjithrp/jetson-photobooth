# PhotoBooth — Current Deployment Specs & State

Snapshot as of **2026-08-15**. Live booth: `pb@192.168.86.30` (LAN), app at `/opt/photobooth`.

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
| Management / SSH / internet | `wlP1p1s0` (onboard) | LAN "Virus-5G", `192.168.86.30/24` — **never used for the AP** |
| Guest hotspot (AP) | `wlx782051871b2c` (USB dongle) | SSID **PhotoBooth** / pass **booth1234**, `192.168.50.1/24`, 2.4 GHz, visible |
| Captive portal | — | `:80` reverse-proxy, **pass-through mode** (guests keep internet; reach photos by scanning the on-screen QR → opens in real browser). `share.base_url = http://192.168.50.1` |

## Feature state
- **Trigger**: gesture, `open_palm` (detects from distance: ≥3 fingers, lowered thresholds). Arduino button wired.
- **Face grouping**: ON (CUDA). **AI background**: OFF. **Gaze correction**: OFF (measure-only scaffold).
- **Printing** (CUPS): OFF.
- **Cloud uploads**: gdrive/S3/FTP all OFF (configured from admin → Sharing; Drive uses in-browser OAuth).

## Key changes (2026-08-15 session)
Apport disabled (`enabled=0` in `/etc/default/apport`, service stopped + disabled) so crash
dialogs never appear on the kiosk. Trigger: one-off cameraDaemon SIGSEGV in its
`Connect_TimeOut` path (auto-recovered); core dump archived at `~/crash-archive/`.
· Wave-twice gesture (`wave`) added. · `camera-usb-kick` boot service resets the Type-C
controller when the Sony is missing from USB after a reboot (stale PC-Remote session).
· Full multi-agent code review → fixed critical/high + medium + low findings and deployed
  (3 commits on `main`, pushed to GitHub):
  - **watchdog** now restarts the real `photobooth-camera` unit and checks the result
    (was a silent no-op against a nonexistent `photobooth-liveview`).
  - **captive proxy** restricted to a guest-route allowlist — the hotspot no longer
    exposes `/api/login`, `/api/gallery`, `/api/settings`, `/api/capture`, `/api/system/*`.
  - **login** per-IP lockout; **delete_session** hardened (can't rmtree the captures root).
  - **service controls** report the true systemctl status; phantom `photobooth-kiosk` gone.
  - blocking test/OAuth calls moved off the event loop; **model caches** capped at one entry
    + load-locked (OOM guard); **capture pipeline** decodes/encodes once per shot;
    **SyncWorker** snapshots-then-merges (no more mid-upload dict race); numeric settings
    bounds-checked; leaked camera-daemon sockets closed; admin face-zone preview stream
    now stops on tab-switch.
  - low tier: strong refs on fire-and-forget capture/print tasks; FTP connection closed
    in `finally`; live preview skips byte-identical frames (+ removed 3 dead liveview fns);
    `_prune` now also clears a session's thumbnails + face-index entries (no more matches
    to deleted 404s); trigger `stop()` joins threads (fixes webcam "camera in use" restart
    race); `timer.interval_seconds` wired as the real between-shots gap; `_email_last` map
    bounded; native shutdown busy-spin → yielding sleep (source only — needs an on-device
    CrSDK rebuild); control/guest/kiosk UI: direct-download gallery, kiosk preview reconnects
    on clean stream-end, guest re-fetches share options per find; misleading no-op admin
    knobs removed (Sony transfer size, keep-local).
  - Test suite 70 green on-device. NOTE: not yet verified with a real Sony capture through
    the rewritten pipeline; native busy-spin fix awaits a `boothCapture`/daemon rebuild.

## Key changes (2026-07-08 session)
Kiosk fullscreen (wmctrl) · gesture worker + distance tuning · gaze scaffold · OOM fix (zram+swap) · jetson_clocks · 1080p render + drop TensorRT EP · fix intermittent numpy/cv2 face-grouping race · kiosk hide-preview-during-processing · Google Drive (OAuth) + S3 uploads · captive Wi-Fi (AP IP fix, http/https auto-detect, pass-through default).

## Admin / source
- Admin UI: `http://192.168.86.30:8000/admin` (PIN-gated).
- Repo: <https://github.com/renjithrp/jetson-photobooth> (public), branch `main`.
  A dev clone lives on the Jetson at `~/development/jetson-photobooth` — separate from
  the running install at `/opt/photobooth`, which is not a git checkout.
- Deploy method: `./deploy/deploy.sh pb@192.168.86.30` from a dev machine (rsync +
  restart of `photobooth`, `photobooth-gesture`, `photobooth-captive`). Add `-n` for a
  dry run, `--deps` to also refresh the venv. Runtime dirs (`venv/`, `gesture-venv/`,
  `data/`, `models/`, `wheels/`, `certs/`) are excluded and survive `--delete`.
