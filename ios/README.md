# PhotoBooth — iPad app (SwiftUI)

A native **iPad-only** app for the booth's kiosk tablet: **live view + trigger**, guest
**find-my-photos by selfie**, **WhatsApp / Google-Drive opt-in**, and a **PIN-gated
admin** area with the WhatsApp send queue. It's a thin client over the booth's
existing HTTP API — no booth-side changes are needed beyond what's already deployed.

> This folder is **source only**. A native app can't be built, signed, or installed
> from the booth repo the way the web pages are — you open it in Xcode and run it on
> your iPad (TestFlight or a development-signed build).

## Requirements
- Xcode 15+, iOS 16+ target.
- The iPad on the **same network as the booth** (the guest hotspot `PhotoBooth`, or
  the booth's LAN).

## Create the Xcode project
1. Xcode → New → Project → **App** (SwiftUI, Swift). Name it `PhotoBooth`.
2. Delete the auto-generated `ContentView.swift` / `…App.swift`.
3. Drag **all files in `PhotoBooth/`** into the project (check "Copy items if needed"
   and add to the app target).
4. Set the required Info.plist keys below, then build & run on the iPad.

### Info.plist — required
The booth serves **plain HTTP** (and self-signed HTTPS) on the LAN, and the app uses
the camera for selfies. Add:

```xml
<key>NSCameraUsageDescription</key>
<string>Take a selfie to find your photos.</string>

<!-- Allow the booth's local HTTP/self-signed address. Scope it to the booth host
     rather than allowing arbitrary loads if you use a fixed IP. -->
<key>NSAppTransportSecurity</key>
<dict>
  <key>NSAllowsLocalNetworking</key><true/>
  <key>NSAllowsArbitraryLoads</key><true/>
</dict>

<!-- iOS 14+ local-network prompt (needed to reach the booth by IP) -->
<key>NSLocalNetworkUsageDescription</key>
<string>Connect to the photo booth on this network.</string>
```

`BoothClient` also trusts the booth host's TLS cert for that host only (a deliberate
LAN-appliance exception, in `TrustDelegate`).

## Configure the booth address
`BoothClient.baseURL` defaults to `http://192.168.50.1` (the guest hotspot). If the
app can't reach the booth, a banner appears — tap it to set the address (e.g. the
booth's LAN IP on `http://<ip>:8000`). It's stored in `@AppStorage`.

## Files
| File | Role |
|------|------|
| `PhotoBoothApp.swift` | App entry; injects the shared `BoothClient`. |
| `BoothClient.swift` | API client (async/await). All endpoints match `backend/main.py`. |
| `Models.swift` | Decodable mirrors of the booth's JSON. |
| `ContentView.swift` | TabView (Booth / My Photos / Admin) + settings sheet. |
| `LiveView.swift` | MJPEG preview via `WKWebView` (WebKit decodes it natively). |
| `CaptureTab.swift` | Live view + Take Photo trigger + camera status. |
| `FindPhotosTab.swift` | Selfie → find → select → WhatsApp / Drive opt-in (+ `ImagePicker`). |
| `AdminTab.swift` | PIN login + WhatsApp send queue (Open WhatsApp / Mark sent). |

## API endpoints used (already live on the booth)
`GET /api/system/info` · `GET /api/preview/stream` · `POST /api/capture` ·
`POST /api/faces/find` · `GET /api/share/options` · `POST /api/share/whatsapp` ·
`POST /api/share/drive` · `POST /api/login` · `GET /api/auth/check` ·
`GET /api/consent/whatsapp/pending` · `POST /api/consent/whatsapp/sent`.

## Notes / decisions
- **Google Drive is opt-IN** (off by default), matching the booth model you chose:
  a photo uploads only if a guest opts it in, once, even for group photos. Your
  original note said "opt-out"; if you want opt-out instead, that's a booth-side
  change (default the photos in, let guests remove) — say the word and I'll flip it.
- **WhatsApp is collect-only**: the app records the number; you send from the Admin
  tab's queue via a `wa.me` link when online. The message carries a **link** to the
  photos, so set a reachable *Public base URL* (or connect Drive/S3) on the booth so
  guests can open it after leaving the booth Wi-Fi. See the booth admin → Sharing.
- **Opening this folder in a plain editor shows Swift errors** ("No such module
  'UIKit'", "Cannot find type…"). That's expected: SourceKit lints against the macOS
  SDK with no project. In an **iOS app target in Xcode** everything resolves.

## Not yet included (easy follow-ups)
- Multi-shot review / gallery browsing (the `/api/gallery` data is available).
- Gesture-trigger status, disk/health tiles in Admin.
- A "remembered devices" flow if you want per-iPad identity.
