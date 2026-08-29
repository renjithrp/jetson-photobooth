import Foundation
import SwiftUI

/// Talks to the PhotoBooth backend (FastAPI on the Jetson). One instance is shared
/// through the environment. The booth serves HTTP (or self-signed HTTPS) on the LAN,
/// so the URLSession delegate trusts the configured booth host — see README for the
/// required Info.plist App Transport Security exception.
///
/// Endpoints mirror backend/main.py exactly:
///   GET  /api/system/info            – health/status
///   GET  /api/preview/stream         – MJPEG live view (rendered by LiveView's WKWebView)
///   POST /api/capture                – trigger a session
///   POST /api/faces/find (multipart) – find-my-photos by selfie
///   GET  /api/share/options          – which share options are enabled
///   POST /api/share/whatsapp         – leave a number for WhatsApp delivery
///   POST /api/share/drive            – opt photos in for Google Drive
///   POST /api/login {pin}            – admin login (sets the session cookie)
///   GET  /api/auth/check             – is the session still valid
///   GET  /api/consent/whatsapp/pending, POST /api/consent/whatsapp/sent (admin)
@MainActor
final class BoothClient: ObservableObject {
    @AppStorage("boothBaseURL") var baseURL: String = "http://192.168.50.1:8000"
    // Auto-join the booth hotspot on launch (defaults match the booth's guest AP).
    @AppStorage("wifiAuto") var wifiAuto: Bool = true
    @AppStorage("wifiSSID") var wifiSSID: String = "PhotoBooth"
    @AppStorage("wifiPass") var wifiPass: String = "booth1234"

    @Published var wifiMessage: String?
    @Published var status: SystemInfo?
    @Published var options: ShareOptions = .init()
    @Published var isAdmin = false
    @Published var lastError: String?

    private lazy var session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.httpCookieStorage = .shared          // keep the admin session cookie
        // Fail fast instead of hanging: waitsForConnectivity once wedged the whole
        // watchdog loop on stale keep-alive sockets after a booth restart ("No booth"
        // forever while the stream kept playing). The watchdog owns recovery.
        cfg.waitsForConnectivity = false
        cfg.timeoutIntervalForRequest = 10
        cfg.timeoutIntervalForResource = 60      // room for selfie uploads over Wi-Fi
        return URLSession(configuration: cfg, delegate: TrustDelegate(host: host),
                          delegateQueue: nil)
    }()

    private var host: String { URL(string: baseURL)?.host ?? "" }

    /// The operator app talks to the backend on :8000 (full API). The booth's captive
    /// portal on :80 only exposes guest routes, so a bare host (no port) is normalized
    /// to :8000 — otherwise live view / trigger / admin / status all 404.
    static func normalized(_ base: String) -> String {
        guard let u = URL(string: base) else { return base }
        if u.port == nil, let h = u.host {
            return "\(u.scheme ?? "http")://\(h):8000"
        }
        return base
    }

    private var effectiveBase: String { Self.normalized(baseURL) }

    func url(_ path: String) -> URL { URL(string: effectiveBase + path)! }

    // MARK: - wi-fi
    /// Join the booth hotspot (if auto-join is on), then refresh. Called on launch
    /// and whenever the watchdog finds the booth unreachable.
    func connectAndRefresh() async {
        if wifiAuto {
            wifiMessage = "Connecting to \(wifiSSID)…"
            if let err = await WiFiManager.join(ssid: wifiSSID, passphrase: wifiPass) {
                wifiMessage = "Wi-Fi: \(err)"
            } else {
                wifiMessage = nil
            }
        }
        await refresh()
    }

    // MARK: - connectivity watchdog
    private var watchdogTask: Task<Void, Never>?

    /// Every ~12s: if the booth stops answering, re-join the hotspot and re-probe —
    /// trying the stored address first, then the default hotspot address (in case the
    /// stored one points at a network the iPad is no longer on). Self-heals dropped
    /// Wi-Fi without anyone touching the iPad.
    func startWatchdog() {
        watchdogTask?.cancel()
        watchdogTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(12))
                guard let self, !Task.isCancelled else { return }
                if await self.probe(self.baseURL) {
                    await self.refresh()
                    continue
                }
                if self.wifiAuto {
                    _ = await WiFiManager.join(ssid: self.wifiSSID, passphrase: self.wifiPass)
                }
                for candidate in [self.baseURL, "http://192.168.50.1:8000"] {
                    if await self.probe(candidate) {
                        if candidate != self.baseURL { self.baseURL = candidate }
                        break
                    }
                }
                await self.refresh()
            }
        }
    }

    /// Short-timeout reachability check (separate session: no connectivity-waiting).
    private lazy var probeSession: URLSession = {
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 3
        cfg.timeoutIntervalForResource = 4
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg, delegate: TrustDelegate(host: host),
                          delegateQueue: nil)
    }()

    /// Also used by Settings to check a typed-in address before saving it.
    func probe(_ base: String) async -> Bool {
        guard let u = URL(string: Self.normalized(base) + "/api/system/info") else { return false }
        guard let (_, resp) = try? await probeSession.data(from: u) else { return false }
        return (resp as? HTTPURLResponse)?.statusCode == 200
    }

    // MARK: - status / options
    func refresh() async {
        async let s: SystemInfo? = get("/api/system/info")
        async let o: ShareOptions? = get("/api/share/options")
        status = await s
        if let o = await o { options = o }
    }

    // MARK: - capture
    func trigger() async -> Bool {
        struct R: Decodable { let ok: Bool }
        return (await post("/api/capture", body: [String: String]()) as R?)?.ok ?? false
    }

    // MARK: - focus
    /// Trigger camera autofocus (same as the web control page's Focus button).
    func focus() async -> Bool {
        struct R: Decodable { var ok: Bool? = nil }
        return (await post("/api/focus", body: [String: String]()) as R?)?.ok ?? false
    }

    // MARK: - gesture trigger tuning (admin)
    func triggerConfig() async -> TriggerConfig? {
        struct S: Decodable { let trigger: TriggerConfig }
        return (await get("/api/settings") as S?)?.trigger
    }

    /// PUT the trigger block (deep-merged server-side). Requires the admin session
    /// cookie (login(pin:)). The gesture worker hot-reloads it within ~3s.
    func saveTrigger(_ t: TriggerConfig) async -> Bool {
        struct Body: Encodable { let trigger: TriggerConfig }
        var req = URLRequest(url: url("/api/settings"))
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONEncoder().encode(Body(trigger: t))
        guard let (_, resp) = try? await session.data(for: req) else { return false }
        return (resp as? HTTPURLResponse)?.statusCode == 200
    }

    // MARK: - wifi info (QR self-download flow)
    func wifiInfo() async -> WifiInfo? { await get("/api/wifi/info") }

    // MARK: - gallery
    /// All photos on the booth, newest session first (real files only — no deleted).
    /// Newest capture session first, or nil if the booth could not be reached.
    ///
    /// The nil matters: collapsing a failed request to an empty list made an
    /// unreachable booth render as "No photos yet", which reads as "the booth took
    /// no pictures" — the opposite of the truth, and it hid real outages.
    func gallerySessions() async -> [GallerySession]? {
        guard let sessions: [GallerySession] = await get("/api/gallery") else { return nil }
        return sessions.sorted { $0.mtime > $1.mtime }
    }

    func gallery() async -> [String] {
        (await gallerySessions() ?? []).flatMap { $0.images }
    }

    /// Opaque ids of devices holding a DHCP lease on the guest hotspot, or nil if
    /// the booth didn't answer. The nil matters: treating a failed poll as "nobody
    /// is connected" would make the next success look like a fresh join.
    func hotspotGuests() async -> [String]? {
        struct R: Decodable { let devices: [String] }
        return (await get("/api/hotspot/guests") as R?)?.devices
    }

    /// Permanently delete photos from the booth. Admin-only server-side, so this
    /// fails with the session cookie missing — the caller checks isAdmin first.
    func deletePhotos(_ photos: [String]) async -> OkResult {
        (await post("/api/gallery/delete", body: ["photos": photos]) as OkResult?)
            ?? OkResult(ok: false, error: "Couldn't reach the booth.")
    }

    /// Fetch photos full-res into a temp dir, keeping their .JPG names so AirDrop
    /// and Files deliver real image files. `progress` reports 1-based completion.
    func downloadToTemp(_ paths: [String],
                        progress: @MainActor (Int, Int) -> Void = { _, _ in }) async -> [URL] {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("booth-share", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var files: [URL] = []
        for (i, p) in paths.enumerated() {
            progress(i + 1, paths.count)
            guard let (data, resp) = try? await URLSession.shared.data(from: url(p)),
                  (resp as? HTTPURLResponse)?.statusCode == 200 else { continue }
            let f = dir.appendingPathComponent((p as NSString).lastPathComponent)
            if (try? data.write(to: f)) != nil { files.append(f) }
        }
        return files
    }

    // MARK: - find my photos
    func findPhotos(selfieJPEG: Data) async -> FindResult {
        var req = URLRequest(url: url("/api/faces/find"))
        req.httpMethod = "POST"
        let boundary = "Boundary-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"selfie\"; filename=\"selfie.jpg\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: image/jpeg\r\n\r\n".data(using: .utf8)!)
        body.append(selfieJPEG)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body
        do {
            let (data, _) = try await session.data(for: req)
            return try JSONDecoder().decode(FindResult.self, from: data)
        } catch {
            return FindResult(matched: false, photos: nil, error: error.localizedDescription)
        }
    }

    // MARK: - guest opt-ins
    private struct WhatsappReq: Encodable { let phone: String; let photos: [String] }
    private struct PhotosReq: Encodable { let photos: [String] }

    func whatsappOptin(phone: String, photos: [String]) async -> OptinResult {
        (await post("/api/share/whatsapp", body: WhatsappReq(phone: phone, photos: photos)) as OptinResult?)
            ?? OptinResult(ok: false, error: "network error")
    }

    func driveOptin(photos: [String]) async -> OptinResult {
        (await post("/api/share/drive", body: PhotosReq(photos: photos)) as OptinResult?)
            ?? OptinResult(ok: false, error: "network error")
    }

    /// Announce a guest's selected photos as a "pending download": after they join
    /// the hotspot via the Wi-Fi QR, the captive popup opens /booth showing a
    /// direct download button — no second QR scan needed.
    func announceDownload(photos: [String]) async {
        struct R: Decodable { let ok: Bool }
        _ = await post("/api/download/announce", body: PhotosReq(photos: photos)) as R?
    }

    // MARK: - system status / service control (admin)
    func networkStatus() async -> NetworkStatus? { await get("/api/network/status") }

    func serviceStates() async -> [String: String] {
        (await get("/api/system/services") as [String: String]?) ?? [:]
    }

    func serviceAction(_ service: String, _ action: String) async -> OkResult {
        (await post("/api/system/service", body: ["service": service, "action": action]) as OkResult?)
            ?? OkResult(ok: false, error: "network error")
    }

    // MARK: - booth upstream Wi-Fi (admin)
    func wifiScan() async -> [WifiNet] { (await get("/api/wifi/scan") as [WifiNet]?) ?? [] }

    func wifiConnect(ssid: String, password: String) async -> OkResult {
        (await post("/api/wifi/connect", body: ["ssid": ssid, "password": password]) as OkResult?)
            ?? OkResult(ok: false, error: "network error")
    }

    func wifiForget(ssid: String) async -> OkResult {
        (await post("/api/wifi/forget", body: ["ssid": ssid]) as OkResult?)
            ?? OkResult(ok: false, error: "network error")
    }

    // MARK: - admin
    func login(pin: String) async -> Bool {
        struct R: Decodable { let ok: Bool }
        let ok = (await post("/api/login", body: ["pin": pin]) as R?)?.ok ?? false
        isAdmin = ok
        return ok
    }

    func checkAuth() async {
        struct R: Decodable { let ok: Bool }
        isAdmin = (await get("/api/auth/check") as R?)?.ok ?? false
    }

    func whatsappPending() async -> [PendingRecipient] {
        (await get("/api/consent/whatsapp/pending") as PendingResponse?)?.pending ?? []
    }

    func markSent(phone: String) async -> Bool {
        struct R: Decodable { let ok: Bool }
        return (await post("/api/consent/whatsapp/sent", body: ["phone": phone]) as R?)?.ok ?? false
    }

    // MARK: - low-level helpers
    private func get<T: Decodable>(_ path: String) async -> T? {
        do {
            let (data, _) = try await session.data(from: url(path))
            return try JSONDecoder().decode(T.self, from: data)
        } catch { lastError = error.localizedDescription; return nil }
    }

    private func post<T: Decodable>(_ path: String, body: any Encodable) async -> T? {
        var req = URLRequest(url: url(path))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONEncoder().encode(AnyEncodable(body))
        do {
            let (data, _) = try await session.data(for: req)
            return try JSONDecoder().decode(T.self, from: data)
        } catch { lastError = error.localizedDescription; return nil }
    }
}

/// Trusts the booth's self-signed cert / plain HTTP on the LAN host ONLY.
/// This is a deliberate LAN-appliance exception, not a general TLS bypass.
private final class TrustDelegate: NSObject, URLSessionDelegate {
    let host: String
    init(host: String) { self.host = host }
    func urlSession(_ s: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        if challenge.protectionSpace.host == host,
           let trust = challenge.protectionSpace.serverTrust {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }
}

/// Type-erased Encodable so `post` can take a heterogeneous JSON body.
private struct AnyEncodable: Encodable {
    let value: any Encodable
    init(_ v: any Encodable) { value = v }
    func encode(to encoder: Encoder) throws { try value.encode(to: encoder) }
}

/// Downloaded files awaiting the iOS share sheet. Shared by the gallery and the
/// full-screen viewer so both present the same sheet.
struct TempFiles: Identifiable {
    let id = UUID()
    let urls: [URL]
}
