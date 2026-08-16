import Foundation
import NetworkExtension

/// Joins the booth's Wi-Fi hotspot by SSID + passphrase using NEHotspotConfiguration
/// (the supported way for an app to join a known network — needs the Hotspot
/// Configuration entitlement). iOS shows a one-time "join Wi-Fi?" prompt the first
/// time; after the guest approves, the app can re-apply it silently.
enum WiFiManager {
    /// Returns nil on success (including "already connected"), else an error message.
    static func join(ssid: String, passphrase: String) async -> String? {
        guard !ssid.isEmpty else { return "no SSID set" }
        let config: NEHotspotConfiguration = passphrase.isEmpty
            ? NEHotspotConfiguration(ssid: ssid)
            : NEHotspotConfiguration(ssid: ssid, passphrase: passphrase, isWEP: false)
        config.joinOnce = false                 // persist so it reconnects automatically
        return await withCheckedContinuation { cont in
            NEHotspotConfigurationManager.shared.apply(config) { error in
                let e = error as NSError?
                if e == nil || e?.code == NEHotspotConfigurationError.alreadyAssociated.rawValue {
                    cont.resume(returning: nil)   // joined, or already on it
                } else {
                    cont.resume(returning: e?.localizedDescription ?? "couldn't join")
                }
            }
        }
    }
}
