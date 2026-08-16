import Foundation

/// Decodable mirrors of the JSON the booth returns. Only the fields the app uses
/// are declared; the backend may send more.

struct SystemInfo: Decodable {
    let busy: Bool
    let daemon_connected: Bool?
    struct Stream: Decodable { let streaming: Bool?; let recovering: Bool?; let fps: Double? }
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

/// GET /api/network/status — booth-side connectivity (management Wi-Fi + hotspot).
struct NetworkStatus: Decodable {
    var internet: String? = nil          // full / limited / none / unknown
    var mgmt_ssid: String? = nil         // upstream Wi-Fi the booth is joined to
    struct Hotspot: Decodable { var active: Bool? = nil; var ssid: String? = nil }
    var hotspot: Hotspot? = nil
}

/// One network from GET /api/wifi/scan.
struct WifiNet: Decodable, Identifiable {
    let ssid: String
    var signal: Int? = nil
    var security: String? = nil
    var in_use: Bool? = nil
    var id: String { ssid }
}

/// POST /api/system/service and /api/wifi/connect|forget result.
struct OkResult: Decodable {
    let ok: Bool
    var error: String? = nil
    var detached: Bool? = nil
}

/// The gesture-trigger block of /api/settings — everything the on-iPad tuning
/// sheet edits. Field names mirror backend/models.py TriggerSettings exactly.
struct TriggerConfig: Codable {
    var gesture_type = "open_palm"
    var gesture_hold_seconds = 1.5
    var gesture_start_delay = 0.0
    var cooldown_seconds = 5.0
    var hand_min_size = 0.08
    var require_face = false
    var show_gesture_overlay = false
    var show_gesture_stats = false
    var tune_mode = false
    var max_hands = 1
    var confirm_frames = 3
    var match_ratio = 0.7
    var hand_face_scale = 0.45
    var assoc_face_dist = 4.0
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
