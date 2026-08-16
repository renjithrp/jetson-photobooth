import Foundation

/// Decodable mirrors of the JSON the booth returns. Only the fields the app uses
/// are declared; the backend may send more.

struct SystemInfo: Decodable {
    let busy: Bool
    let daemon_connected: Bool?
    struct Stream: Decodable { let streaming: Bool?; let fps: Double? }
    let camera_stream: Stream?
    let booth: String?          // not sent by backend; kept optional for future use
}

struct ShareOptions: Decodable {
    var email = false
    var links = false
    var whatsapp = false
    var drive_optin = false
}

struct FindResult: Decodable {
    let matched: Bool
    let photos: [String]?
    let error: String?
}

struct OptinResult: Decodable {
    let ok: Bool
    var added: Int? = nil
    var total: Int? = nil
    let error: String?
}

struct PendingRecipient: Decodable, Identifiable {
    let phone: String
    let raw: String
    let count: Int
    let photos: [String]
    let wa_link: String
    let download_url: String
    var id: String { phone }
}

struct PendingResponse: Decodable {
    let pending: [PendingRecipient]
    let count: Int
}

/// One capture session from GET /api/gallery (only real files on disk — deleted
/// sessions never appear here).
struct GallerySession: Decodable {
    let session: String
    let mtime: Double
    let images: [String]
}

/// GET /api/wifi/info — hotspot details + QR codes (data: URIs) for the guest
/// self-download flow: join_qr joins the booth Wi-Fi, find_qr opens the photo finder.
struct WifiInfo: Decodable {
    var active: Bool? = nil
    var ssid: String? = nil
    var join_qr: String? = nil
    var find_qr: String? = nil
    var find_url: String? = nil
}
