import SwiftUI
import UIKit

/// All booth photos (from /api/gallery — real files only, no deleted). Tap to select;
/// "Filter by selfie" narrows to just the tapped guest's photos. Selected photos can be
/// downloaded, sent to WhatsApp, or saved to Drive.
struct GalleryView: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) var dismiss

    @State private var all: [String] = []
    @State private var filter: Set<String>?          // nil = show everything
    @State private var selected: Set<String> = []
    @State private var showCamera = false
    @State private var busy = false
    @State private var message: String?
    @State private var showPhone = false
    @State private var phone = ""
    @State private var viewer: ViewerStart?          // photo view (pager) start index

    struct ViewerStart: Identifiable { let id: Int }

    private let cols = [GridItem(.adaptive(minimum: 104), spacing: 6)]
    private var shown: [String] { filter == nil ? all : all.filter { filter!.contains($0) } }
    private var chosen: [String] { shown.filter { selected.contains($0) } }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if let m = message { Text(m).font(.footnote).foregroundStyle(.secondary).padding(6) }
                if busy && all.isEmpty { Spacer(); ProgressView(); Spacer() }
                else if shown.isEmpty {
                    Spacer()
                    Text(filter == nil ? "No photos yet." : "No photos matched that selfie.")
                        .foregroundStyle(.secondary)
                    Spacer()
                } else {
                    ScrollView {
                        LazyVGrid(columns: cols, spacing: 6) {
                            ForEach(shown, id: \.self) { u in cell(u) }
                        }.padding(6)
                    }
                }
                if !selected.isEmpty { shareBar }
            }
            .navigationTitle(filter == nil ? "Gallery (\(all.count))" : "Your photos (\(shown.count))")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Close") { dismiss() } }
                ToolbarItem(placement: .topBarTrailing) {
                    HStack(spacing: 14) {
                        if !shown.isEmpty {
                            Button { viewer = ViewerStart(id: 0) } label: {
                                Label("Photo view", systemImage: "rectangle.expand.vertical")
                            }
                        }
                        if filter == nil {
                            Button { showCamera = true } label: { Label("Filter by selfie", systemImage: "person.crop.circle.badge.questionmark") }
                        } else {
                            Button("Show all") { filter = nil; selected = [] }
                        }
                    }
                }
            }
            .fullScreenCover(item: $viewer) { v in
                PhotoPagerView(photos: shown, selected: $selected, start: v.id)
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
            .task { await load() }
        }
        // abandoned gallery returns to the live view; paused while a child screen is up
        .idleReturn(paused: showCamera || showPhone || viewer != nil)
    }

    private func cell(_ u: String) -> some View {
        AsyncImage(url: booth.url(u.replacingOccurrences(of: "/captures/", with: "/thumbs/"))) { img in
            img.resizable().scaledToFill()
        } placeholder: { Color.gray.opacity(0.2) }
        .frame(width: 104, height: 104).clipped().cornerRadius(8)
        .onTapGesture {                       // tap the photo -> open the zoomable viewer
            if let i = shown.firstIndex(of: u) { viewer = ViewerStart(id: i) }
        }
        .overlay(alignment: .topTrailing) {   // tap the circle -> select/unselect
            Button {
                if selected.contains(u) { selected.remove(u) } else { selected.insert(u) }
            } label: {
                Image(systemName: selected.contains(u) ? "checkmark.circle.fill" : "circle")
                    .font(.title3)
                    .foregroundStyle(selected.contains(u) ? .green : .white)
                    .shadow(radius: 2).padding(6)
            }.buttonStyle(.plain)
        }
    }

    private var shareBar: some View {
        HStack {
            Button { download() } label: { Label("Download", systemImage: "square.and.arrow.down") }
                .buttonStyle(.bordered)
            if booth.options.drive_optin {
                Button { Task { await optDrive() } } label: { Label("Drive", systemImage: "arrow.up.doc") }
                    .buttonStyle(.bordered)
            }
            if booth.options.whatsapp {
                Button { showPhone = true } label: { Label("WhatsApp", systemImage: "message.fill") }
                    .buttonStyle(.borderedProminent)
            }
        }
        .disabled(busy)
        .padding(10).frame(maxWidth: .infinity).background(.thinMaterial)
    }

    // MARK: - actions
    private func load() async {
        busy = true; all = await booth.gallery(); busy = false
    }
    private func filterBySelfie(_ jpeg: Data) async {
        busy = true; message = "Finding your photos…"
        let r = await booth.findPhotos(selfieJPEG: jpeg)
        busy = false
        if r.matched, let p = r.photos, !p.isEmpty {
            filter = Set(p); selected = Set(shown); message = nil
        } else {
            message = r.error ?? "No match — try a clearer, well-lit selfie."
        }
    }
    private func download() {
        guard !chosen.isEmpty else { return }
        let qs = chosen.map { "p=" + ($0.addingPercentEncoding(withAllowedCharacters: .urlQueryValueAllowed) ?? $0) }.joined(separator: "&")
        UIApplication.shared.open(booth.url("/api/download?" + qs))
    }
    private func optWhatsapp() async {
        busy = true
        let r = await booth.whatsappOptin(phone: phone, photos: chosen)
        busy = false
        message = r.ok ? "Saved — will send \(chosen.count) photo(s) via WhatsApp." : (r.error ?? "Couldn't save.")
    }
    private func optDrive() async {
        busy = true
        let r = await booth.driveOptin(photos: chosen)
        busy = false
        message = r.ok ? "Saved to Google Drive (uploads when online)." : (r.error ?? "Couldn't save to Drive.")
    }
}

extension CharacterSet {
    static let urlQueryValueAllowed: CharacterSet = {
        var cs = CharacterSet.urlQueryAllowed
        cs.remove(charactersIn: "&=?/")
        return cs
    }()
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
