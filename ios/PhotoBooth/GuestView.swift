import CoreImage
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
    @State private var viewer: GalleryView.ViewerStart?   // zoomable photo view

    private let cols = [GridItem(.adaptive(minimum: 110), spacing: 8)]
    private var chosen: [String] { photos.filter { selected.contains($0) } }

    var body: some View {
        NavigationStack {
            Group {
                switch step {
                case .choice:  choice
                case .results: results
                case .qr:      QRStepsView(downloadPhotos: chosen,
                                           onDone: { step = photos.isEmpty ? .choice : .results })
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
        .fullScreenCover(item: $viewer) { v in
            PhotoPagerView(photos: photos, selected: $selected, start: v.id)
        }
        .sheet(isPresented: $showPhonePanel) {
            PhoneEntryView(photoCount: chosen.count) { num in
                phone = num
                Task { await optWhatsapp() }
            }
        }
        .task { await booth.refresh() }   // pick up newly-enabled share options live
        // pause while a child screen is up — it runs its own idle timer
        .idleReturn(paused: showCamera || showPhonePanel || viewer != nil)
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
            Label("We found \(photos.count) photo\(photos.count == 1 ? "" : "s") of you",
                  systemImage: "party.popper.fill")
                .font(.largeTitle.bold()).padding(.top, 8)
            Text("Tap a photo to unselect it, then choose how to get them.")
                .font(.callout).foregroundStyle(.secondary)

            ScrollView {
                LazyVGrid(columns: cols, spacing: 8) {
                    ForEach(photos, id: \.self) { u in cell(u) }
                }.padding(.horizontal)
            }

            if let m = message { Text(m).font(.headline).foregroundStyle(.green) }

            HStack(spacing: 14) {
                if booth.options.whatsapp {
                    actionButton(waSaved ? "WhatsApp saved" : "Get on WhatsApp",
                                 icon: "message.fill", done: waSaved, prominent: true) {
                        showPhonePanel = true
                    }
                }
                if booth.options.drive_optin {
                    actionButton(driveSaved ? "Drive saved" : "Save to Google Drive",
                                 icon: "arrow.up.doc.fill", done: driveSaved) {
                        Task { await optDrive() }
                    }
                }
                actionButton("Instant download to phone", icon: "qrcode") { step = .qr }
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
        .opacity(selected.contains(u) ? 1 : 0.45)
        .onTapGesture {                       // tap the photo -> zoomable viewer
            if let i = photos.firstIndex(of: u) { viewer = GalleryView.ViewerStart(id: i) }
        }
        .overlay(alignment: .topTrailing) {   // tap the circle -> select/unselect
            Button {
                if selected.contains(u) { selected.remove(u) } else { selected.insert(u) }
            } label: {
                Image(systemName: selected.contains(u) ? "checkmark.circle.fill" : "circle")
                    .font(.title3)
                    .foregroundStyle(selected.contains(u) ? .green : .white)
                    .shadow(radius: 2).padding(5)
            }.buttonStyle(.plain)
        }
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
/// 2) scan the photo QR. When the guest already found their photos on the iPad,
/// step 2 is a DIRECT download link for those photos (QR generated on-device) — no
/// second selfie. Only without a prior selfie does it fall back to the finder page.
struct QRStepsView: View {
    @EnvironmentObject var booth: BoothClient
    var downloadPhotos: [String] = []
    var onDone: () -> Void
    @State private var info: WifiInfo?
    @State private var page = 0
    @State private var autoAdvanced = false

    /// Short link to the chosen photos, on the guest-facing base (derived from
    /// find_url so it matches what phones on the hotspot can reach).
    ///
    /// Deliberately NOT the full /api/download?p=…&p=… URL: that grew ~74 bytes per
    /// photo and past roughly 37 photos exceeded what a QR can encode, so
    /// CIQRCodeGenerator returned nil and the guest was told the code couldn't be
    /// created. The booth already has the list — announceDownload() sends it in
    /// .task — so /d resolves to exactly that download, and the code stays small
    /// and coarse enough to scan quickly.
    private var directURL: String? {
        guard !downloadPhotos.isEmpty else { return nil }
        let base = info?.find_url?.replacingOccurrences(of: "/booth", with: "")
            ?? booth.url("").absoluteString
        return base + "/d"
    }

    var body: some View {
        VStack(spacing: 22) {
            if let info {
                Text(page == 0 ? "Step 1 of 2 — Join the booth Wi-Fi"
                               : (directURL != nil ? "Step 2 of 2 — Download your photos"
                                                   : "Step 2 of 2 — Open your photos"))
                    .font(.largeTitle.bold())

                if page == 1 && autoAdvanced {
                    Label("Connected to the booth Wi-Fi", systemImage: "checkmark.circle.fill")
                        .font(.title3.bold()).foregroundStyle(.green)
                        .transition(.opacity)
                }

                if page == 0 {
                    qrImageView(Self.decode(info.join_qr),
                                missing: "The booth Wi-Fi is off — ask the staff, or connect to \(info.ssid ?? "the booth network") manually.")
                    instruction(downloadPhotos.isEmpty
                        ? "Open your phone's camera, point it at the code, and tap “Join Network”."
                        : "Open your phone's camera, scan, and tap “Join Network” — your download page pops up by itself. If it doesn't, tap Next for a direct code.")
                } else if let direct = directURL {
                    qrImageView(Self.qrCode(from: direct),
                                missing: "Couldn't create the download code — ask the staff.")
                    instruction("Scan with your phone's camera — your \(downloadPhotos.count) photo\(downloadPhotos.count == 1 ? "" : "s") download straight away.")
                } else {
                    qrImageView(Self.decode(info.find_qr),
                                missing: "The photo page isn't available right now — ask the staff.")
                    instruction("Scan with your phone's camera and open the link — then take a selfie there to find and download your photos.")
                }

                HStack(spacing: 16) {
                    // Back always available: page 1 -> page 0; page 0 -> photos/choice
                    Button {
                        if page == 1 { page = 0 } else { onDone() }
                    } label: { Label("Back", systemImage: "chevron.left").font(.title3.bold()).padding(6) }
                        .buttonStyle(.bordered).controlSize(.large)
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
        .task {
            info = await booth.wifiInfo()
            if !downloadPhotos.isEmpty {
                // arm the captive-popup download banner for the phone about to join
                await booth.announceDownload(photos: downloadPhotos)
            }
            await watchForJoin()
        }
    }

    /// Move to step 2 by itself once a phone joins the hotspot.
    ///
    /// The guest has no way to tell the booth they joined — their phone shows no
    /// confirmation — so they were left on step 1 unsure whether the scan worked.
    /// The booth watches Wi-Fi associations and advances for them.
    ///
    /// Only a join that happened AFTER this screen appeared counts.
    ///
    /// "Associated within the last 25s" was too loose: a phone that joined shortly
    /// before the guest reached this screen — very often the booth iPad itself,
    /// which re-joins the hotspot every time the app launches — instantly skipped
    /// step 1. Comparing the join's age against how long the screen has been open
    /// makes "after I started showing this QR" the actual condition. (The booth now
    /// also excludes the caller from its own view, so the iPad can't see itself.)
    private func watchForJoin() async {
        let openedAt = Date()
        while !Task.isCancelled {
            let onScreen = Date().timeIntervalSince(openedAt)
            if page == 0, let ages = await booth.hotspotGuestAges(),
               ages.contains(where: { Double($0) < onScreen }) {
                withAnimation { autoAdvanced = true; page = 1 }
            }
            try? await Task.sleep(for: .seconds(2))
        }
    }

    private func qrImageView(_ img: UIImage?, missing: String) -> some View {
        Group {
            if let img {
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

    /// Generate a QR code on-device (CoreImage) for the direct-download link.
    static func qrCode(from string: String) -> UIImage? {
        guard let data = string.data(using: .utf8),
              let filter = CIFilter(name: "CIQRCodeGenerator") else { return nil }
        filter.setValue(data, forKey: "inputMessage")
        // lower correction for long URLs keeps the code coarse enough to scan
        filter.setValue(string.count > 900 ? "L" : "M", forKey: "inputCorrectionLevel")
        guard let ci = filter.outputImage?.transformed(by: CGAffineTransform(scaleX: 12, y: 12)),
              let cg = CIContext().createCGImage(ci, from: ci.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}
