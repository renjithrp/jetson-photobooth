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
        cfg.waitsForConnectivity = true
        return URLSession(configuration: cfg, delegate: TrustDelegate(host: host),
                          delegateQueue: nil)
    }()

    private var host: String { URL(string: baseURL)?.host ?? "" }

    /// The operator app talks to the backend on :8000 (full API). The booth's captive
    /// portal on :80 only exposes guest routes, so a bare host (no port) is normalized
    /// to :8000 — otherwise live view / trigger / admin / status all 404.
    private var effectiveBase: String {
        guard let u = URL(string: baseURL) else { return baseURL }
        if u.port == nil, let h = u.host {
            return "\(u.scheme ?? "http")://\(h):8000"
        }
        return baseURL
    }

    func url(_ path: String) -> URL { URL(string: effectiveBase + path)! }

    // MARK: - wi-fi
    /// Join the booth hotspot (if auto-join is on), then refresh. Called on launch.
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

    // MARK: - wifi info (QR self-download flow)
    func wifiInfo() async -> WifiInfo? { await get("/api/wifi/info") }

    // MARK: - gallery
    /// All photos on the booth, newest session first (real files only — no deleted).
    func gallery() async -> [String] {
        let sessions: [GallerySession] = await get("/api/gallery") ?? []
        return sessions.sorted { $0.mtime > $1.mtime }.flatMap { $0.images }
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
