import SwiftUI
import UIKit

/// Guest self-service flow, kiosk-friendly (big type, few steps):
///   1. Choice: "Take a selfie to find your photos"  or  "Download on your phone (QR)"
///   2. Selfie -> their photos, all selected -> WhatsApp number / Google Drive opt-in
///   3. QR steps: join booth Wi-Fi (QR) then open the photo page (QR), with instructions
/// Idle for 10s returns to the live view (cancellable countdown).
struct GuestView: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) var dismiss

    enum Step { case choice, results, qr }
    @State private var step: Step = .choice
    @State private var showCamera = false
    @State private var busy = false
    @State private var message: String?
    @State private var photos: [String] = []
    @State private var selected: Set<String> = []
    @State private var phone = ""
    @State private var showPhonePanel = false
    @State private var waSaved = false
    @State private var driveSaved = false

    private let cols = [GridItem(.adaptive(minimum: 110), spacing: 8)]
    private var chosen: [String] { photos.filter { selected.contains($0) } }

    var body: some View {
        NavigationStack {
            Group {
                switch step {
                case .choice:  choice
                case .results: results
                case .qr:      QRStepsView(onDone: { step = photos.isEmpty ? .choice : .results })
                }
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { dismiss() } label: { Label("Camera", systemImage: "chevron.left").font(.title3) }
                }
            }
        }
        .sheet(isPresented: $showCamera) {
            ImagePicker(source: .camera, camera: .front) { data in
                if let data { Task { await find(data) } }
            }.ignoresSafeArea()
        }
        .idleReturn()
    }

    // MARK: - step 1: choice
    private var choice: some View {
        VStack(spacing: 26) {
            Spacer()
            Text("Get your photos").font(.system(size: 40, weight: .bold))
            Text("Take a quick selfie — we'll find every photo you're in.")
                .font(.title3).foregroundStyle(.secondary)
            if busy { ProgressView().controlSize(.large) }
            if let m = message { Text(m).font(.title3).foregroundStyle(.orange) }

            bigButton("Take a selfie", icon: "camera.fill", prominent: true) { showCamera = true }
            bigButton("Download on your phone", icon: "qrcode") { step = .qr }
            Spacer()
        }
        .padding(40)
    }

    // MARK: - step 2: results + share choices
    private var results: some View {
        VStack(spacing: 14) {
            Text("We found \(photos.count) photo\(photos.count == 1 ? "" : "s") of you 🎉")
                .font(.largeTitle.bold()).padding(.top, 8)
            Text("Tap a photo to unselect it, then choose how to get them.")
                .font(.callout).foregroundStyle(.secondary)

            ScrollView {
                LazyVGrid(columns: cols, spacing: 8) {
                    ForEach(photos, id: \.self) { u in cell(u) }
                }.padding(.horizontal)
            }

            if let m = message { Text(m).font(.headline).foregroundStyle(.green) }

            if showPhonePanel {
                HStack(spacing: 10) {
                    TextField("Your WhatsApp number, incl. country code", text: $phone)
                        .keyboardType(.phonePad).font(.title3)
                        .padding(12).background(.quaternary, in: RoundedRectangle(cornerRadius: 12))
                    Button { Task { await optWhatsapp() } } label: {
                        Text("Save").font(.title3.bold())
                    }.buttonStyle(.borderedProminent).controlSize(.large).disabled(busy)
                }.padding(.horizontal, 30)
            }

            HStack(spacing: 14) {
                if booth.options.whatsapp {
                    actionButton(waSaved ? "WhatsApp ✓" : "Get on WhatsApp",
                                 icon: "message.fill", done: waSaved, prominent: true) {
                        showPhonePanel.toggle()
                    }
                }
                if booth.options.drive_optin {
                    actionButton(driveSaved ? "Drive ✓" : "Save to Google Drive",
                                 icon: "arrow.up.doc.fill", done: driveSaved) {
                        Task { await optDrive() }
                    }
                }
                actionButton("Download (QR)", icon: "qrcode") { step = .qr }
            }
            .disabled(busy || chosen.isEmpty)
            .padding(.bottom, 6)

            Button("Start over") { reset() }.font(.callout)
        }
        .padding(.bottom, 10)
    }

    private func cell(_ u: String) -> some View {
        AsyncImage(url: booth.url(u.replacingOccurrences(of: "/captures/", with: "/thumbs/"))) { img in
            img.resizable().scaledToFill()
        } placeholder: { Color.gray.opacity(0.2) }
        .frame(width: 110, height: 110).clipped().cornerRadius(10)
        .overlay(alignment: .topTrailing) {
            Image(systemName: selected.contains(u) ? "checkmark.circle.fill" : "circle")
                .font(.title3)
                .foregroundStyle(selected.contains(u) ? .green : .white).padding(5)
        }
        .opacity(selected.contains(u) ? 1 : 0.45)
        .onTapGesture { if selected.contains(u) { selected.remove(u) } else { selected.insert(u) } }
    }

    // MARK: - buttons
    private func bigButton(_ title: String, icon: String, prominent: Bool = false,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon)
                .font(.title2.bold())
                .frame(maxWidth: 480).padding(.vertical, 18)
        }
        .buttonStyle(prominent ? AnyButtonStyle(.borderedProminent) : AnyButtonStyle(.bordered))
        .controlSize(.large)
    }

    private func actionButton(_ title: String, icon: String, done: Bool = false,
                              prominent: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: done ? "checkmark.circle.fill" : icon)
                .font(.headline).padding(.vertical, 10).padding(.horizontal, 4)
        }
        .buttonStyle(prominent && !done ? AnyButtonStyle(.borderedProminent) : AnyButtonStyle(.bordered))
        .tint(done ? .green : nil)
        .controlSize(.large)
    }

    // MARK: - actions
    private func reset() {
        step = .choice; photos = []; selected = []; message = nil
        waSaved = false; driveSaved = false; showPhonePanel = false; phone = ""
    }

    private func find(_ jpeg: Data) async {
        busy = true; message = "Looking for you…"
        let r = await booth.findPhotos(selfieJPEG: jpeg)
        busy = false
        if r.matched, let p = r.photos, !p.isEmpty {
            photos = p; selected = Set(p); message = nil; step = .results
        } else {
            message = r.error ?? "No match yet — try again with your face well lit."
        }
    }

    private func optWhatsapp() async {
        busy = true
        let r = await booth.whatsappOptin(phone: phone, photos: chosen)
        busy = false
        if r.ok {
            waSaved = true; showPhonePanel = false
            message = "Done! Your \(chosen.count) photo\(chosen.count == 1 ? "" : "s") will arrive on WhatsApp."
        } else {
            message = r.error ?? "Couldn't save your number — check it and try again."
        }
    }

    private func optDrive() async {
        busy = true
        let r = await booth.driveOptin(photos: chosen)
        busy = false
        if r.ok { driveSaved = true; message = "Saved! They'll upload to Google Drive." }
        else { message = r.error ?? "Couldn't save to Drive." }
    }
}

/// Type-erased button style so a ternary can pick bordered vs prominent.
struct AnyButtonStyle: PrimitiveButtonStyle {
    private let make: (Configuration) -> AnyView
    init<S: PrimitiveButtonStyle>(_ style: S) {
        make = { AnyView(Button($0).buttonStyle(style)) }
    }
    func makeBody(configuration: Configuration) -> some View { make(configuration) }
}

// MARK: - QR self-download steps

/// Step-by-step self-download: 1) scan the Wi-Fi QR to join the booth network,
/// 2) scan the photo QR to open the finder page on their phone.
struct QRStepsView: View {
    @EnvironmentObject var booth: BoothClient
    var onDone: () -> Void
    @State private var info: WifiInfo?
    @State private var page = 0

    var body: some View {
        VStack(spacing: 22) {
            if let info {
                Text(page == 0 ? "Step 1 of 2 — Join the booth Wi-Fi"
                               : "Step 2 of 2 — Open your photos")
                    .font(.largeTitle.bold())

                if page == 0 {
                    qr(info.join_qr, missing: "The booth Wi-Fi is off — ask the staff, or connect to \(info.ssid ?? "the booth network") manually.")
                    instruction("Open your phone's camera, point it at the code, and tap “Join Network”.")
                } else {
                    qr(info.find_qr, missing: "The photo page isn't available right now — ask the staff.")
                    instruction("Scan with your phone's camera and open the link — then take a selfie there to find and download your photos.")
                }

                HStack(spacing: 16) {
                    if page == 1 {
                        Button { page = 0 } label: { Label("Back", systemImage: "chevron.left").font(.title3.bold()).padding(6) }
                            .buttonStyle(.bordered).controlSize(.large)
                    }
                    if page == 0 {
                        Button { page = 1 } label: {
                            Label("I'm connected — next", systemImage: "chevron.right").font(.title3.bold()).padding(6)
                        }.buttonStyle(.borderedProminent).controlSize(.large)
                    } else {
                        Button { onDone() } label: { Text("Done").font(.title3.bold()).padding(6) }
                            .buttonStyle(.borderedProminent).controlSize(.large)
                    }
                }
            } else {
                ProgressView().controlSize(.large)
                Text("Loading…").foregroundStyle(.secondary)
            }
        }
        .padding(36)
        .task { info = await booth.wifiInfo() }
    }

    private func qr(_ dataURI: String?, missing: String) -> some View {
        Group {
            if let img = Self.decode(dataURI) {
                Image(uiImage: img).resizable().interpolation(.none)
                    .frame(width: 320, height: 320)
                    .background(.white).cornerRadius(16).shadow(radius: 8)
            } else {
                Text(missing).font(.title3).foregroundStyle(.orange)
                    .multilineTextAlignment(.center).frame(maxWidth: 480)
            }
        }
    }

    private func instruction(_ t: String) -> some View {
        Text(t).font(.title3).foregroundStyle(.secondary)
            .multilineTextAlignment(.center).frame(maxWidth: 560)
    }

    /// "data:image/png;base64,...." -> UIImage
    static func decode(_ uri: String?) -> UIImage? {
        guard let uri, uri.hasPrefix("data:"), let comma = uri.firstIndex(of: ",") else { return nil }
        guard let d = Data(base64Encoded: String(uri[uri.index(after: comma)...])) else { return nil }
        return UIImage(data: d)
    }
}
