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
| Captive portal | — | `:80` reverse-proxy, **probe-only hijack** (`photobooth-captive.conf`): only the OS probe domains resolve to the booth, so **guests keep real internet** and the sign-in sheet still fires on join — the only thing iOS opens automatically. The sheet lands on **`/welcome`**, whose sole job is handing the guest to Safari (`x-safari-http://`), because the sign-in window cannot open the camera. It has NO success/"connected" screen: that was tried and removed as pure friction. Three routes in, weakest first: auto-redirect (meta refresh + JS — observed being IGNORED, WebKit wants a user gesture for an app scheme), tap anywhere (whole body carries the link, so any tap is that gesture), and the explicit button. Written fallback steps below. The full hijack is off, backed up at `/root/captive-full-hijack.conf.disabled`; `setup-captive.sh install probe|proxy|offline` switches modes. Guests are firewalled off every RFC1918 net (`deploy/guest-lan-block` NM dispatcher, re-inserted at the head of FORWARD on every AP up) so they can't reach the LAN or the booth's admin on it. **Guest-facing name: `photos.internal`** (also `photobooth.internal`) → 192.168.50.1, served by the hotspot's dnsmasq (`deploy/booth-hostname.conf`); `.internal` is ICANN-reserved so it can never collide with a real domain. `share.base_url = http://photos.internal`, so both the photos QR and the post-capture share QR carry the name. ⚠ It resolves ONLY through the booth's resolver — a phone on encrypted DNS (private DNS profile, DoH, iCloud Private Relay) gets NXDOMAIN where the IP would have worked, which matters now that guests have their own internet. The IP stays the fallback: `/welcome` shows it under the name, and the "Open photo booth" button uses it. |

## Feature state
- **Trigger**: gesture, `wave` (open palm swung side-to-side, 3 alternating swings ≈ waving
  twice; ~20 fps sampling for wave, 6 fps for static poses). Arduino button wired.
- **Face grouping**: ON (CUDA). **AI background**: OFF. **Gaze correction**: OFF (measure-only scaffold).
- **Printing** (CUPS): OFF.
- **Cloud uploads**: S3/FTP OFF. **Google Drive CONNECTED** (2026-08-16; OAuth web client
  865464360538-…, `drive.file` scope, folder `PhotoBooth`, share links on). Connect/reconnect
  recipe: Google only accepts localhost redirect URIs now → `ssh -N -L 8000:localhost:8000
  pb@<booth>` then Connect from `http://localhost:8000/admin` (network-independent).
  OAuth app published to production 2026-08-16. ⚠ One step pending: reconnect Drive once
  (Connect in admin) so the token is re-issued under production status — the current token
  was minted during "Testing" and may still carry that mode's 7-day expiry.
- **Google Drive guest opt-in**: ON and verified live — "Save to Drive" (iPad app + guest
  page) queues via the sync worker; per-photo dedup (a group shot uploads once).
- **WhatsApp opt-in**: ON (collect-only), verified end-to-end. Admin send queue (admin →
  Sharing, or iOS app ⋯ → Admin): **"Send with cloud links"** uploads the guest's pending
  photos to Drive under `guests/<phone>/` and opens wa.me with **ONE public folder link**
  (per-file links are the S3/fallback path). Idempotent + deduped: re-opt-ins add only new
  photos, the folder link never changes, sent photos are never re-queued. Keyed to the
  normalized phone number — typos create separate guests.
- **Guest self-download**: photos selected on the iPad are announced as a pending
  download, and step 2 of the iPad's QR flow is a DIRECT download QR for exactly
  those photos. With no captive sheet there is no automatic popup, so the join is
  confirmed from the booth side instead: `GET /api/hotspot/guests` returns opaque
  ids taken from the hotspot's DHCP leases, and a newly-seen device (a) lights
  "✓ Phone connected — scan this one next" on the kiosk attract screen and (b) makes
  the **iPad advance from QR 1 to QR 2 by itself**. The guest's phone never tells
  the booth anything — the DHCP lease does. Enablers: full DNS hijack (see Network table —
  probe-only proved flaky on iOS), the kiosk iPad's probes are
  answered with "Success" so it never sees the sheet (reserved 192.168.50.203,
  BOOTH_KIOSK_IPS drop-in), captive proxy retries stale sockets after backend restarts.
  The direct-download QR remains as step-2 fallback.
- **Clock guard**: captures hold up to 10 s for NTP sync after boot (no more 1969 sessions);
  failed captures remove their empty session folder.
- **Camera Wi-Fi fallback: NOT possible with the A7R IV.** cameraDaemon was rebuilt
  on-device (busy-spin fix now live) with a network fallback (`BOOTH_CAMERA_IP`/`_MAC`
  env → `CreateCameraObjectInfoEthernetConnection`), and it was tested live — the SDK
  returns `Api_NotSupportModelOfEthernet` for ILCE-7RM4A: the model is in the header
  enum but the runtime only allows network remote for newer bodies (7RM5/A7IV/A1/FX3…).
  Its Wi-Fi PC Remote only talks to Sony's Imaging Edge. Even spoofing a
  network-capable model id (7M4/7RM5) past the local check was probed live: object
  creation succeeds but Connect() fails instantly (0x8000) — no protocol path exists.
  The fallback code stays in the
  daemon (dormant, env unset) and works as-is if the camera is ever upgraded to a
  network-capable body; the camera's hotspot MAC e8:4f:25:fd:af:47 has a dnsmasq
  reservation at 192.168.50.29. Backup binary at external/crsdk/cameraDaemon.bak.
  Practical USB backup instead: a spare USB cable + the second USB port (the
  camera-usb-kick service already self-heals the stale-session case). After the test the
  camera was returned to PC Remote > USB and normal operation verified (reconnect + live
  view at ~15 fps). To (re)test the fallback with a future network-capable body: camera in
  PC Remote > Wi-Fi on the PhotoBooth hotspot → drop-in
  `photobooth-camera.service.d/wifi-fallback.conf` with `BOOTH_CAMERA_IP/_MAC` →
  `daemon-reload` + restart with USB unplugged → journal shows "trying Wi-Fi fallback"
  then "Connected"; finish with a test capture, then remove the drop-in / replug USB.

## iPad app (kiosk tablet)
**On TestFlight since 2026-08-28**: 1.0 (build 1), uploaded via `xcrun altool` with the
App Store Connect API key; `ITSAppUsesNonExemptEncryption=false` is declared so builds
don't stall on export compliance. Bump `CURRENT_PROJECT_VERSION` in `ios/project.yml`
for every further upload. ⚠ The app is inert without a booth on the same Wi-Fi — an
external tester (or a Beta App reviewer) just sees "Can't reach the booth".

Native SwiftUI kiosk app in `ios/` — **iPad-only** (TARGETED_DEVICE_FAMILY=2). Source +
XcodeGen spec; regenerate with `xcodegen generate`, build via `xcodebuild` — see
`ios/README.md`. Installed on the iPad Air 11", dev-signed with team `J6QU4CTJD7`,
bundle `com.renjithrp.photobooth`. (An early iPhone install from testing can be deleted
off the phone like any app.)
- Full-screen camera-style booth screen: live view (WKWebView MJPEG, auto-reconnects after
  sheets/foreground), round shutter, ⋯ menu (grouped: Photo gallery · Booth setup ·
  Reconnect, which now reports whether it worked), recent-photo gallery button, big
  "Get your photos" guest CTA.
- Guest flow: selfie → matched photos → WhatsApp (dedicated big-keypad number screen) /
  Drive opt-in / direct-download QR (zip link QR generated on-device — no second selfie).
  QR self-download steps: Wi-Fi join QR then photo QR, with instructions.
- Gallery: **full-screen** (not an iPad form sheet), a flat newest-first grid **3 per
  row** with no date/session headers. Browsing and selecting are separate modes (tap
  opens, "Select" or long-press selects the whole tile) with a large 46pt selection
  badge; Select all; pull-to-refresh; selfie filter; tile + full-photo view with
  pinch/double-tap zoom; AirDrop shows per-photo progress; results report as
  self-dismissing toasts rather than grey footnote text. **Delete** removes the
  selected photos (admin-only, behind a confirmation alert) via
  `POST /api/gallery/delete`, which also drops their thumbnails and face-index
  entries and removes a session emptied by the delete. An unreachable booth says so
  (with the address and a Try again) instead of rendering as "No photos yet"; photos
  captured while the gallery is open are announced as a "N new photos — tap to show"
  bar rather than spliced in under the reader's finger; the full-screen viewer can
  AirDrop/download the photo on screen without going back to the tiles.
- Kiosk behaviors: auto-joins the booth hotspot on open (NEHotspotConfiguration), screen
  never auto-locks, 10 s idle → cancellable 5 s countdown (with mini live view + "Go to
  camera") unwinds to the booth screen.
- Talks to the backend on **:8000** (the captive :80 only exposes guest routes).
- Self-healing: a connectivity watchdog probes every ~12 s and re-joins Wi-Fi / re-finds
  the booth (fail-fast HTTP timeouts — a booth restart can't wedge it), and the live view
  reconnects itself via a load-event heartbeat (~10 s after a stream drop) instead of
  freezing while the status pill stays green.

## Key changes (2026-08-28 session)
App icon added (six-blade aperture, booth teal): one geometry source in `tools/make_icon.py`
emits `frontend/assets/icon.svg` + favicon/PWA/apple-touch rasters and the iPad
`AppIcon.appiconset` (opaque 1024). `/favicon.ico` now serves a real file; all four web
pages link the icon, and the captive proxy allowlist gained `/assets/` (static frontend
files only) so the guest page can load it. · iPad gallery + ⋯ menu usability pass (see the
iPad app section above), verified in the iPad Air simulator against a mock booth.

## Key changes (2026-08-16 session)
Google Drive connected (localhost-redirect OAuth via SSH tunnel) · WhatsApp send queue
"Send with cloud links": one Drive folder link per guest (`guests/<phone>`), per-file
fallback, all verified live incl. returning-guest merge/dedup · one-scan guest download:
probe-domain DNS hijack + pending-download announce + server-side banner on the captive
sheet + kiosk-iPad probe exemption · captive portal survives backend restarts (fresh
pending lookups, proxy retry) · iPad app: connectivity watchdog + fail-fast timeouts +
live-view heartbeat reconnect; iPad-only target; guest flow (selfie → WhatsApp keypad /
Drive opt-in / instant-download QR), photo viewer with pinch zoom, 10 s idle-return with
cancellable countdown + mini live view, auto Wi-Fi join, keep-awake · test data purged
(consent store, Drive folder, sync queue) — clean baseline for the next event.

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
  - Test suite green on-device (80+). Real Sony capture verified through the rewritten
    pipeline (60 MP JPEG, byte-identical no-effects path). Native busy-spin fix still
    awaits a `boothCapture`/daemon rebuild.

## Key changes (2026-08-15 session — guest sharing + iOS app)
- **Gesture worker settings bug fixed**: it could never read the root-owned settings.json
  (PermissionError silently pinned it to `open_palm`, ignoring ALL trigger config). Now
  loads the trigger block from the backend API. Wave tuned (0.4 hand-lengths/swing, 1.5 s
  idle, ~20 fps sampling) and kiosk prompts "Wave 👋 to start!".
- **Consent + dedup engine** (`backend/consent.py`, persisted to `data/consent.json`):
  WhatsApp collect-only opt-in and per-photo Google Drive opt-in; a photo is never
  delivered twice on either channel (group photos upload/send once). New guest endpoints
  (`/api/share/whatsapp`, `/api/share/drive` — captive-allowlisted) + admin send queue
  (`/api/consent/whatsapp/*`, wa.me links, mark-sent) with UI in admin → Sharing. Drive
  excluded from the automatic per-session upload (opt-in only). Guest web page gained the
  opt-in buttons.
- **iOS app built and installed** (see section above): booth screen, guest flow, QR
  self-download, gallery + zoom, PIN admin, auto Wi-Fi join, idle return, keep-awake.
- **Data hygiene**: `faces/find` filters photos whose files were deleted (no 404 matches);
  zip download clamps pre-1980 file timestamps (1969-mtime photos 500'd the whole zip);
  captures hold for NTP sync (no new 1969 sessions); the 5 old `session_19691231_*`
  sessions renamed to `session_20260815_00xx00` with face-index/consent remapped; failed
  captures clean up their empty folder (6 existing empties removed).

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
