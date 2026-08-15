import SwiftUI
import UIKit

/// Selfie → find-my-photos → opt in for WhatsApp / Google Drive.
/// Mirrors the web guest page, but native (camera + share).
struct FindPhotosTab: View {
    @EnvironmentObject var booth: BoothClient
    @State private var showCamera = false
    @State private var busy = false
    @State private var message: String?
    @State private var photos: [String] = []
    @State private var selected: Set<String> = []
    @State private var showPhone = false
    @State private var phone = ""

    private let cols = [GridItem(.adaptive(minimum: 96), spacing: 6)]

    var body: some View {
        NavigationStack {
            VStack(spacing: 12) {
                if let m = message { Text(m).font(.callout).foregroundStyle(.secondary) }

                if photos.isEmpty {
                    Spacer()
                    Button { showCamera = true } label: {
                        Label("Take a selfie to find your photos", systemImage: "camera.fill")
                            .font(.headline).padding()
                    }.buttonStyle(.borderedProminent).disabled(busy)
                    if busy { ProgressView() }
                    Spacer()
                } else {
                    ScrollView {
                        LazyVGrid(columns: cols, spacing: 6) {
                            ForEach(photos, id: \.self) { u in
                                AsyncImage(url: booth.url(thumb(u))) { img in
                                    img.resizable().scaledToFill()
                                } placeholder: { Color.gray.opacity(0.2) }
                                .frame(width: 96, height: 96).clipped().cornerRadius(8)
                                .overlay(alignment: .topTrailing) { tick(u) }
                                .onTapGesture { toggle(u) }
                            }
                        }.padding(6)
                    }
                    shareBar
                }
            }
            .navigationTitle("My Photos")
            .sheet(isPresented: $showCamera) {
                ImagePicker(source: .camera, camera: .front) { data in
                    if let data { Task { await find(data) } }
                }.ignoresSafeArea()
            }
            .alert("Send to WhatsApp", isPresented: $showPhone) {
                TextField("Phone incl. country code", text: $phone).keyboardType(.phonePad)
                Button("Save") { Task { await optWhatsapp() } }
                Button("Cancel", role: .cancel) {}
            } message: { Text("We'll send your \(selected.count) selected photo(s) when the booth is online.") }
        }
    }

    private var shareBar: some View {
        HStack {
            if booth.options.drive_optin {
                Button { Task { await optDrive() } } label: {
                    Label("Save to Drive", systemImage: "arrow.up.doc")
                }.buttonStyle(.bordered).disabled(selected.isEmpty || busy)
            }
            if booth.options.whatsapp {
                Button { showPhone = true } label: {
                    Label("WhatsApp", systemImage: "message.fill")
                }.buttonStyle(.borderedProminent).disabled(selected.isEmpty || busy)
            }
        }.padding()
    }

    private func tick(_ u: String) -> some View {
        Image(systemName: selected.contains(u) ? "checkmark.circle.fill" : "circle")
            .foregroundStyle(selected.contains(u) ? .green : .white).padding(4)
    }

    private func toggle(_ u: String) {
        if selected.contains(u) { selected.remove(u) } else { selected.insert(u) }
    }

    private func thumb(_ u: String) -> String { u.replacingOccurrences(of: "/captures/", with: "/thumbs/") }
    private var chosen: [String] { photos.filter { selected.contains($0) } }

    private func find(_ jpeg: Data) async {
        busy = true; message = "Finding your photos…"
        let r = await booth.findPhotos(selfieJPEG: jpeg)
        busy = false
        if r.matched, let p = r.photos, !p.isEmpty {
            photos = p; selected = Set(p); message = "Found \(p.count) photo(s) of you"
        } else {
            message = r.error ?? "No match — try a clearer, well-lit selfie."
        }
    }

    private func optWhatsapp() async {
        busy = true
        let r = await booth.whatsappOptin(phone: phone, photos: chosen)
        busy = false
        message = r.ok ? "Saved — your photos will arrive on WhatsApp soon."
                       : (r.error ?? "Couldn't save your number.")
    }

    private func optDrive() async {
        busy = true
        let r = await booth.driveOptin(photos: chosen)
        busy = false
        message = r.ok ? "Saved to Google Drive (uploads when online)."
                       : (r.error ?? "Couldn't save to Drive.")
    }
}

/// Minimal UIImagePickerController wrapper for a front-camera selfie.
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
            onImage(img?.jpegData(compressionQuality: 0.85))
            p.dismiss(animated: true)
        }
        func imagePickerControllerDidCancel(_ p: UIImagePickerController) {
            onImage(nil); p.dismiss(animated: true)
        }
    }
}
