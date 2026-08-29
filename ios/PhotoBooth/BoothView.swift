import SwiftUI
import UIKit

/// Full-screen, camera-app style booth screen: live preview edge to edge, a round
/// shutter on the right, a ⋯ menu (Admin / Settings) top-right, a gallery button
/// bottom-left, and a big "Get your photos" guest call-to-action bottom-center.
/// The screen never auto-locks while the app is frontmost (kiosk).
struct BoothView: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.scenePhase) private var scenePhase
    @State private var showGuest = false
    @State private var showGallery = false
    @State private var showAdmin = false
    @State private var showSettings = false
    @State private var showTune = false
    @State private var triggering = false
    @State private var recentThumb: String?
    @State private var liveReload = 0        // bump to force the MJPEG stream to reconnect
    @State private var focusing = false      // tap-to-focus reticle is showing
    @State private var toast: ToastMessage?  // reconnect / capture feedback

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            LiveView(url: booth.url("/api/preview/stream"), reloadID: liveReload).ignoresSafeArea()
                .contentShape(Rectangle())
                .onTapGesture { Task { await focusTap() } }   // tap anywhere = autofocus

            // camera-app style focus reticle while the lens locks
            if focusing {
                RoundedRectangle(cornerRadius: 10)
                    .stroke(.yellow, lineWidth: 3)
                    .frame(width: 96, height: 96)
                    .shadow(radius: 4)
                    .transition(.opacity)
            }

            // top bar: status (left) + ⋯ menu (right)
            VStack {
                HStack(alignment: .top) {
                    statusPill
                    Spacer()
                    // Grouped by how often it gets used: photos every session, booth
                    // setup occasionally, recovery when something is wrong. A flat
                    // five-item list made "Reconnect" as prominent as "Gallery".
                    Menu {
                        Section {
                            Button { showGallery = true } label: {
                                Label("Photo gallery", systemImage: "photo.on.rectangle")
                            }
                        }
                        Section("Booth setup") {
                            Button { showTune = true } label: {
                                Label("Gesture tuning", systemImage: "hand.raised")
                            }
                            Button { showAdmin = true } label: {
                                Label("Booth admin", systemImage: "lock.shield")
                            }
                            Button { showSettings = true } label: {
                                Label("App settings", systemImage: "gearshape")
                            }
                        }
                        Section(booth.status == nil ? "Booth not responding" : "Connected") {
                            Button { Task { await reconnect() } } label: {
                                Label("Reconnect to booth", systemImage: "arrow.clockwise")
                            }
                        }
                    } label: { circleIcon("ellipsis") }
                }
                Spacer()
            }
            .padding()

            // gallery (bottom-left) + guest CTA (bottom-center) + shutter (right, centered)
            HStack {
                VStack {
                    Spacer()
                    Button { showGallery = true } label: { galleryButton }
                }
                Spacer()
                Button { Task { await shoot() } } label: {
                    ShutterButton(busy: triggering || (booth.status?.busy ?? false))
                }
                .disabled(triggering || (booth.status?.busy ?? false))
                .padding(.trailing, 24)
            }
            .padding()

            // big friendly guest call-to-action
            VStack {
                Spacer()
                Button { showGuest = true } label: {
                    Label("Get your photos", systemImage: "sparkles")
                        .font(.title2.bold()).foregroundStyle(.white)
                        .padding(.horizontal, 26).padding(.vertical, 14)
                        .background(.blue.opacity(0.9)).clipShape(Capsule())
                        .shadow(radius: 8)
                }
                .padding(.bottom, 26)
            }
        }
        .statusBarHidden(true)
        .toast($toast)
        .fullScreenCover(isPresented: $showGuest, onDismiss: { liveReload += 1 }) { GuestView() }
        // Full screen, not .sheet: on iPad a sheet is a ~540pt form sheet, which
        // wasted most of the display and squeezed the photo grid.
        .fullScreenCover(isPresented: $showGallery,
                         onDismiss: { liveReload += 1; Task { await loadThumb() } }) { GalleryView() }
        .sheet(isPresented: $showAdmin, onDismiss: { liveReload += 1 }) { AdminView() }
        .sheet(isPresented: $showSettings, onDismiss: { liveReload += 1 }) { SettingsSheet() }
        // half-height so the live view + detection overlay stay visible while tuning
        .sheet(isPresented: $showTune, onDismiss: { liveReload += 1 }) {
            TriggerTuneView().presentationDetents([.medium, .large])
        }
        .onChange(of: scenePhase) { phase in
            // reconnect Wi-Fi + booth + stream, keep the screen awake, on every foreground
            if phase == .active {
                liveReload += 1
                UIApplication.shared.isIdleTimerDisabled = true
                Task { await booth.connectAndRefresh() }
            }
        }
        .onAppear { UIApplication.shared.isIdleTimerDisabled = true }   // never auto-lock in the booth
        .task { await loadThumb() }
    }

    // MARK: - pieces
    private func circleIcon(_ name: String) -> some View {
        Image(systemName: name).font(.title2.bold()).foregroundStyle(.white)
            .frame(width: 46, height: 46).background(.black.opacity(0.45)).clipShape(Circle())
    }

    private var statusPill: some View {
        let cs = booth.status?.camera_stream
        let streaming = cs?.streaming ?? false
        let recovering = cs?.recovering ?? false
        // A brief self-healing USB drop shows a neutral "Reconnecting…", not a red
        // "Camera offline" — only a sustained outage goes red.
        let state: (String, String, Color) =
            booth.status == nil ? ("No booth", "exclamationmark.triangle", .red)
            : streaming        ? ("Live", "dot.radiowaves.left.and.right", .green)
            : recovering       ? ("Reconnecting…", "arrow.triangle.2.circlepath", .orange)
            :                    ("Camera offline", "video.slash", .red)
        return Label(state.0, systemImage: state.1)
            .font(.caption.bold()).foregroundStyle(.white)
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(state.2.opacity(0.85))
            .clipShape(Capsule())
            .onTapGesture { if booth.status == nil { showSettings = true } }
    }

    private var galleryButton: some View {
        Group {
            if let t = recentThumb {
                AsyncImage(url: booth.url(t.replacingOccurrences(of: "/captures/", with: "/thumbs/"))) { img in
                    img.resizable().scaledToFill()
                } placeholder: { Color.gray.opacity(0.3) }
            } else {
                Image(systemName: "photo.on.rectangle").font(.title).foregroundStyle(.white)
                    .frame(maxWidth: .infinity, maxHeight: .infinity).background(.black.opacity(0.45))
            }
        }
        .frame(width: 60, height: 60).clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(.white, lineWidth: 2))
    }

    // MARK: - actions
    private func shoot() async {
        triggering = true
        _ = await booth.trigger()
        try? await Task.sleep(for: .seconds(6))     // countdown + capture + process
        // Poll until the session actually ends (processing/review can outlast the
        // 6s guess; the watchdog only refreshes every ~12s, which left the shutter
        // stuck on "busy" long after the capture finished).
        for _ in 0..<15 {
            await booth.refresh()
            if booth.status?.busy != true { break }
            try? await Task.sleep(for: .seconds(2))
        }
        await loadThumb()
        triggering = false
    }
    /// "Reconnect" used to be silent — you tapped it and nothing visibly happened,
    /// so staff tapped it repeatedly. Say what the outcome was.
    private func reconnect() async {
        toast = ToastMessage(text: "Reconnecting…")
        await booth.connectAndRefresh()
        toast = booth.status == nil
            ? ToastMessage(text: "Still can't reach the booth — check App settings.", kind: .error)
            : ToastMessage(text: "Connected to the booth.", kind: .success)
    }
    private func focusTap() async {
        guard !focusing else { return }
        withAnimation(.easeIn(duration: 0.1)) { focusing = true }
        _ = await booth.focus()
        withAnimation(.easeOut(duration: 0.3)) { focusing = false }
    }
    private func loadThumb() async { recentThumb = await booth.gallery().first }
}

/// iOS-camera-style round shutter.
struct ShutterButton: View {
    let busy: Bool
    var body: some View {
        ZStack {
            Circle().stroke(.white, lineWidth: 5).frame(width: 82, height: 82)
            Circle().fill(busy ? .gray : .white).frame(width: 66, height: 66)
            if busy { ProgressView().tint(.black) }
        }
    }
}
