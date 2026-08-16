import Foundation
import Photos
import SwiftUI

/// Automatically saves new booth photos into the iPad's Photos library.
///
/// Polls the booth gallery every 20s while the app runs; anything not yet saved is
/// downloaded full-res and added to Photos (add-only permission — the app never
/// reads the library). Saved capture paths are remembered in UserDefaults so each
/// photo lands exactly once, across launches. Toggle lives in Settings
/// ("Auto-save new photos"), on by default.
@MainActor
final class PhotoSync: ObservableObject {
    static let shared = PhotoSync()
    @Published var lastMessage: String?

    private var task: Task<Void, Never>?
    private let savedKey = "syncedPhotoPaths"
    private var syncing = false

    func start(booth: BoothClient) {
        task?.cancel()
        task = Task { [weak booth] in
            while !Task.isCancelled {
                if UserDefaults.standard.object(forKey: "autoSyncPhotos") as? Bool ?? true,
                   let booth {
                    await self.syncOnce(booth: booth)
                }
                try? await Task.sleep(for: .seconds(20))
            }
        }
    }

    func syncOnce(booth: BoothClient) async {
        guard !syncing else { return }
        syncing = true
        defer { syncing = false }

        let photos = await booth.gallery()
        var saved = Set(UserDefaults.standard.stringArray(forKey: savedKey) ?? [])
        // First run on a booth with an existing gallery: adopt the backlog silently
        // instead of bulk-dumping hundreds of old photos into the iPad.
        if saved.isEmpty && photos.count > 30 {
            UserDefaults.standard.set(photos, forKey: savedKey)
            return
        }
        let fresh = photos.filter { !saved.contains($0) }
        guard !fresh.isEmpty else { return }

        let status = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        guard status == .authorized || status == .limited else {
            lastMessage = "Photos access denied — enable it in iOS Settings"
            return
        }
        var added = 0
        for path in fresh.prefix(30) {           // cap per cycle; the rest follow next poll
            guard let (data, resp) = try? await URLSession.shared.data(from: booth.url(path)),
                  (resp as? HTTPURLResponse)?.statusCode == 200, data.count > 10_000
            else { continue }
            do {
                try await PHPhotoLibrary.shared().performChanges {
                    let req = PHAssetCreationRequest.forAsset()
                    req.addResource(with: .photo, data: data, options: nil)
                }
                saved.insert(path)
                added += 1
            } catch { continue }                 // retried on the next cycle
        }
        UserDefaults.standard.set(Array(saved), forKey: savedKey)
        if added > 0 { lastMessage = "Saved \(added) new photo\(added == 1 ? "" : "s") to Photos" }
    }
}
