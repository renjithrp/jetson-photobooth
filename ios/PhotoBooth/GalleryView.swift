import SwiftUI
import UIKit

/// All booth photos (from /api/gallery — real files only, no deleted ones), grouped
/// by capture session so a wall of thumbnails reads as "the shots we just took".
///
/// Browsing and selecting are separate modes, the way Photos.app does it: tapping a
/// photo opens it, and "Select" switches the whole tile into a selection target.
/// (Previously every tile carried a small always-on circle in its corner — a ~30pt
/// target that was easy to miss and easy to hit by accident.) Selected photos can be
/// AirDropped, downloaded, sent to WhatsApp, or saved to Drive.
struct GalleryView: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) var dismiss

    @State private var sessions: [GallerySession] = []
    @State private var filter: Set<String>?          // nil = show everything
    @State private var selected: Set<String> = []
    @State private var selecting = false             // tile tap selects instead of opens
    @State private var showCamera = false
    @State private var busy = false
    @State private var loaded = false
    @State private var toast: ToastMessage?
    @State private var progress: String?             // "Preparing 3 of 8…"
    @State private var showPhone = false
    @State private var phone = ""
    @State private var viewer: ViewerStart?          // photo view (pager) start index
    @State private var visible = 60                  // pagination: photos shown so far
    @State private var shareURLs: TempFiles?         // downloaded files for AirDrop/share
    @State private var unreachable = false           // booth didn't answer, vs genuinely empty
    @State private var pending: [GallerySession]?    // newer photos, waiting to be shown
    @State private var pendingCount = 0
    @State private var confirmDelete = false

    struct ViewerStart: Identifiable { let id: Int }

    /// Three per row, fixed rather than adaptive: on an 11" iPad that gives a tile
    /// wide enough to judge a photo from, and the count stays the same in portrait
    /// and landscape instead of reflowing.
    private let cols = Array(repeating: GridItem(.flexible(), spacing: 8), count: 3)

    // MARK: - derived lists
    private func photos(in s: GallerySession) -> [String] {
        filter == nil ? s.images : s.images.filter { filter!.contains($0) }
    }
    /// Sessions that still have something to show under the current filter.
    private var shownSessions: [GallerySession] { sessions.filter { !photos(in: $0).isEmpty } }
    /// Every visible photo, in display order — what the pager and "Select all" act on.
    private var flatShown: [String] { shownSessions.flatMap { photos(in: $0) } }
    /// One flat newest-first wall of photos — sessions still order the list, they
    /// just aren't drawn as separate groups.
    private var pagedPhotos: [String] { Array(flatShown.prefix(visible)) }
    private var chosen: [String] { flatShown.filter { selected.contains($0) } }
    private var allSelected: Bool { !flatShown.isEmpty && flatShown.allSatisfy { selected.contains($0) } }

    private var navTitle: String {
        if selecting && !selected.isEmpty { return "\(selected.count) selected" }
        if filter != nil { return "Your photos (\(flatShown.count))" }
        return "Gallery (\(flatShown.count))"
    }

    var body: some View {
        NavigationStack {
            Group {
                if !loaded && sessions.isEmpty {
                    VStack(spacing: 12) { ProgressView(); Text("Loading photos…").foregroundStyle(.secondary) }
                } else if unreachable && sessions.isEmpty {
                    unreachableState
                } else if shownSessions.isEmpty {
                    emptyState
                } else {
                    grid
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .safeAreaInset(edge: .top) { if pendingCount > 0 { newPhotosBar } }
            .safeAreaInset(edge: .bottom) { if !chosen.isEmpty { shareBar } }
            .navigationTitle(navTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(selecting ? "Done" : "Close") {
                        if selecting { selecting = false; selected.removeAll() } else { dismiss() }
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    HStack(spacing: 18) {
                        if selecting {
                            Button(allSelected ? "Deselect all" : "Select all") {
                                if allSelected { selected.removeAll() }
                                else { selected.formUnion(flatShown) }
                            }
                        } else {
                            if filter == nil {
                                Button { showCamera = true } label: {
                                    Label("Find by selfie", systemImage: "person.crop.circle.badge.questionmark")
                                }
                            } else {
                                Button("Show all") { filter = nil; selected = [] }
                            }
                            if !flatShown.isEmpty { Button("Select") { selecting = true } }
                        }
                    }
                }
            }
            .fullScreenCover(item: $viewer) { v in
                PhotoPagerView(photos: flatShown, selected: $selected, start: v.id)
            }
            .sheet(isPresented: $showCamera) {
                ImagePicker(source: .camera, camera: .front) { data in
                    if let data { Task { await filterBySelfie(data) } }
                }.ignoresSafeArea()
            }
            .sheet(isPresented: $showPhone) {
                PhoneEntryView(photoCount: chosen.count) { num in
                    phone = num
                    Task { await optWhatsapp() }
                }
            }
            .sheet(item: $shareURLs) { s in ShareSheet(items: s.urls) }
            .task { await watchForNewPhotos() }
            .alert("Delete \(chosen.count) photo\(chosen.count == 1 ? "" : "s")?",
                   isPresented: $confirmDelete) {
                Button("Delete", role: .destructive) { Task { await deleteSelected() } }
                Button("Cancel", role: .cancel) { }
            } message: {
                Text("This permanently removes them from the booth, including any "
                     + "thumbnails and face matches. It can't be undone.")
            }
            .toast($toast)
            // picking a photo inside the full-screen viewer means the guest wants to
            // act on it, so bring the grid along into selection mode
            .onChange(of: selected) { sel in if !sel.isEmpty { selecting = true } }
            .task { await load() }
        }
        // abandoned gallery returns to the live view; paused while a child screen is up
        .idleReturn(paused: showCamera || showPhone || viewer != nil || shareURLs != nil)
    }

    // MARK: - pieces
    private var grid: some View {
        ScrollView {
            LazyVGrid(columns: cols, spacing: 8) {
                ForEach(pagedPhotos, id: \.self) { cell($0) }
            }
            .padding(10)
            if flatShown.count > pagedPhotos.count {
                ProgressView()
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .onAppear { visible += 60 }
            }
        }
        .refreshable { await load() }
    }

    private func thumbImage(_ u: String) -> some View {
        AsyncImage(url: booth.url(u.replacingOccurrences(of: "/captures/", with: "/thumbs/"))) { img in
            img.resizable().scaledToFill()
        } placeholder: { Color.clear }
    }

    private func cell(_ u: String) -> some View {
        let isSel = selected.contains(u)
        return Rectangle()
            .fill(Color.gray.opacity(0.15))
            .aspectRatio(1, contentMode: .fit)          // square tiles that fill the column
            .overlay { thumbImage(u) }
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .overlay {
                if selecting && isSel {
                    RoundedRectangle(cornerRadius: 10).strokeBorder(Color.accentColor, lineWidth: 3)
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if selecting {
                    // Sized to be unmissable and easy to hit at arm's length on the
                    // booth iPad; the whole tile toggles, so this is the indicator
                    // rather than the only target.
                    Image(systemName: isSel ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 46, weight: .semibold))
                        .foregroundStyle(isSel ? Color.green : .white)
                        .background(Circle().fill(.black.opacity(0.45)).padding(4))
                        .shadow(radius: 3)
                        .padding(10)
                }
            }
            .opacity(selecting && !isSel ? 0.6 : 1)
            .contentShape(Rectangle())
            .onTapGesture {
                if selecting { toggle(u) }
                else if let i = flatShown.firstIndex(of: u) { viewer = ViewerStart(id: i) }
            }
            .onLongPressGesture {            // long-press starts selecting, like Photos
                if !selecting { selecting = true }
                toggle(u)
            }
            .accessibilityLabel(isSel ? "Photo, selected" : "Photo")
    }

    /// Photos taken while the gallery is open are announced rather than spliced in:
    /// inserting a new session at the top would shove the grid down under the
    /// reader's finger, and silently reorder a selection in progress.
    private var newPhotosBar: some View {
        Button {
            if let p = pending { sessions = p }
            pending = nil; pendingCount = 0
        } label: {
            Label("\(pendingCount) new photo\(pendingCount == 1 ? "" : "s") — tap to show",
                  systemImage: "arrow.down.circle.fill")
                .font(.subheadline.weight(.semibold))
                .padding(.horizontal, 16).padding(.vertical, 10)
                .background(.thinMaterial, in: Capsule())
        }
        .padding(.top, 6)
    }

    private var unreachableState: some View {
        VStack(spacing: 10) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 46)).foregroundStyle(.secondary)
            Text("Can't reach the booth").font(.headline)
            Text("The iPad isn't getting a reply from \(booth.baseURL). Check it's on the "
                 + "booth Wi-Fi, or use Reconnect from the ⋯ menu.")
                .font(.subheadline).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Try again") { Task { await load() } }
                .buttonStyle(.borderedProminent).padding(.top, 4)
        }
        .padding(40)
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: filter == nil ? "photo.on.rectangle.angled"
                                            : "person.crop.circle.badge.questionmark")
                .font(.system(size: 46)).foregroundStyle(.secondary)
            Text(filter == nil ? "No photos yet" : "No photos matched that selfie")
                .font(.headline)
            Text(filter == nil
                 ? "Photos appear here as soon as the booth takes them."
                 : "Try again with a clearer, well-lit selfie facing the camera.")
                .font(.subheadline).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            if filter != nil {
                Button("Show all photos") { filter = nil; selected = [] }
                    .buttonStyle(.bordered).padding(.top, 4)
            }
        }
        .padding(40)
    }

    private var shareBar: some View {
        VStack(spacing: 8) {
            if let p = progress {
                HStack(spacing: 8) { ProgressView(); Text(p).font(.footnote).foregroundStyle(.secondary) }
            }
            HStack(spacing: 10) {
                Text("\(chosen.count) selected").font(.subheadline.weight(.semibold))
                Button("Clear") { selected.removeAll() }.font(.subheadline)
                Spacer()
                Button { Task { await airdrop() } } label: { Label("AirDrop", systemImage: "square.and.arrow.up") }
                    .buttonStyle(.bordered)
                Button { download() } label: { Label("Download", systemImage: "square.and.arrow.down") }
                    .buttonStyle(.bordered)
                Button(role: .destructive) {
                    // Deleting is admin-gated on the booth; say so up front rather
                    // than letting the request come back 401 after a confirmation.
                    if booth.isAdmin { confirmDelete = true }
                    else { toast = ToastMessage(text: "Log in first: ⋯ → Booth admin.", kind: .error) }
                } label: { Label("Delete", systemImage: "trash") }
                    .buttonStyle(.bordered).tint(.red)
                if booth.options.drive_optin {
                    Button { Task { await optDrive() } } label: { Label("Drive", systemImage: "arrow.up.doc") }
                        .buttonStyle(.bordered)
                }
                if booth.options.whatsapp {
                    Button { showPhone = true } label: { Label("WhatsApp", systemImage: "message.fill") }
                        .buttonStyle(.borderedProminent)
                }
            }
        }
        .disabled(busy)
        .padding(.horizontal, 14).padding(.vertical, 10)
        .frame(maxWidth: .infinity)
        .background(.bar)
    }

    // MARK: - actions
    private func toggle(_ u: String) {
        if selected.contains(u) { selected.remove(u) } else { selected.insert(u) }
    }

    private func load() async {
        busy = true
        let fetched = await booth.gallerySessions()
        unreachable = (fetched == nil)
        if let fetched { sessions = fetched }
        pending = nil; pendingCount = 0
        loaded = true
        busy = false
        // a session deleted from the admin must not stay selected (and then 404 on send)
        selected.formIntersection(Set(sessions.flatMap { $0.images }))
    }

    /// Quiet poll so a capture taken while someone is browsing doesn't go unnoticed.
    private func watchForNewPhotos() async {
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(15))
            guard !Task.isCancelled, let latest = await booth.gallerySessions() else { continue }
            let have = Set(sessions.flatMap { $0.images })
            let fresh = latest.flatMap { $0.images }.filter { !have.contains($0) }
            if !fresh.isEmpty { pending = latest; pendingCount = fresh.count }
        }
    }

    private func filterBySelfie(_ jpeg: Data) async {
        busy = true
        toast = ToastMessage(text: "Finding your photos…")
        let r = await booth.findPhotos(selfieJPEG: jpeg)
        busy = false
        if r.matched, let p = r.photos, !p.isEmpty {
            filter = Set(p)
            selected = Set(flatShown)
            selecting = true
            toast = ToastMessage(text: "Found \(flatShown.count) photo\(flatShown.count == 1 ? "" : "s") of you.",
                                 kind: .success)
        } else {
            toast = ToastMessage(text: r.error ?? "No match — try a clearer, well-lit selfie.",
                                 kind: .error)
        }
    }

    /// Download the selected photos full-res to temp files, then present the iOS
    /// share sheet (AirDrop, Messages, Mail…). File URLs keep the .JPG names so
    /// AirDrop delivers proper image files.
    private func airdrop() async {
        guard !chosen.isEmpty else { return }
        let want = chosen
        busy = true
        let files = await booth.downloadToTemp(want) { done, total in
            progress = "Preparing \(done) of \(total)…"
        }
        progress = nil
        busy = false
        if files.isEmpty {
            toast = ToastMessage(text: "Couldn't fetch the photos — is the booth reachable?", kind: .error)
        } else {
            if files.count < want.count {
                toast = ToastMessage(text: "\(want.count - files.count) photo(s) couldn't be fetched.", kind: .error)
            }
            shareURLs = TempFiles(urls: files)
        }
    }

    private func download() {
        guard !chosen.isEmpty else { return }
        let qs = chosen.map { "p=" + ($0.addingPercentEncoding(withAllowedCharacters: .urlQueryValueAllowed) ?? $0) }
            .joined(separator: "&")
        UIApplication.shared.open(booth.url("/api/download?" + qs))
    }

    private func deleteSelected() async {
        let doomed = chosen
        guard !doomed.isEmpty else { return }
        busy = true
        let r = await booth.deletePhotos(doomed)
        busy = false
        if r.ok {
            selected.subtract(doomed)
            await load()                       // re-read rather than trust local state
            toast = ToastMessage(text: "Deleted \(doomed.count) photo\(doomed.count == 1 ? "" : "s").",
                                 kind: .success)
        } else {
            toast = ToastMessage(text: r.error ?? "Couldn't delete.", kind: .error)
        }
    }

    private func optWhatsapp() async {
        busy = true
        let n = chosen.count
        let r = await booth.whatsappOptin(phone: phone, photos: chosen)
        busy = false
        toast = r.ok
            ? ToastMessage(text: "Saved — \(n) photo\(n == 1 ? "" : "s") queued for WhatsApp.", kind: .success)
            : ToastMessage(text: r.error ?? "Couldn't save.", kind: .error)
    }

    private func optDrive() async {
        busy = true
        let r = await booth.driveOptin(photos: chosen)
        busy = false
        toast = r.ok
            ? ToastMessage(text: "Saved to Google Drive (uploads when online).", kind: .success)
            : ToastMessage(text: r.error ?? "Couldn't save to Drive.", kind: .error)
    }
}

extension CharacterSet {
    static let urlQueryValueAllowed: CharacterSet = {
        var cs = CharacterSet.urlQueryAllowed
        cs.remove(charactersIn: "&=?/")
        return cs
    }()
}

/// iOS share sheet (AirDrop, Messages, Mail…) for downloaded photo files.
struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_ c: UIActivityViewController, context: Context) {}
}

/// Front-camera selfie picker.
struct ImagePicker: UIViewControllerRepresentable {
    let source: UIImagePickerController.SourceType
    var camera: UIImagePickerController.CameraDevice = .front
    let onImage: (Data?) -> Void

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let c = UIImagePickerController()
        c.sourceType = source
        if source == .camera { c.cameraDevice = camera }
        c.delegate = context.coordinator
        return c
    }
    func updateUIViewController(_ c: UIImagePickerController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(onImage: onImage) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let onImage: (Data?) -> Void
        init(onImage: @escaping (Data?) -> Void) { self.onImage = onImage }
        func imagePickerController(_ p: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            let img = info[.originalImage] as? UIImage
            onImage(img?.jpegData(compressionQuality: 0.85)); p.dismiss(animated: true)
        }
        func imagePickerControllerDidCancel(_ p: UIImagePickerController) { onImage(nil); p.dismiss(animated: true) }
    }
}
