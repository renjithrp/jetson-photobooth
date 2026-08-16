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
    @State private var triggering = false
    @State private var recentThumb: String?
    @State private var liveReload = 0        // bump to force the MJPEG stream to reconnect

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            LiveView(url: booth.url("/api/preview/stream"), reloadID: liveReload).ignoresSafeArea()

            // top bar: status (left) + ⋯ menu (right)
            VStack {
                HStack(alignment: .top) {
                    statusPill
                    Spacer()
                    Menu {
                        Button { showGallery = true } label: { Label("Gallery", systemImage: "photo.on.rectangle") }
                        Button { showAdmin = true } label: { Label("Admin", systemImage: "lock.shield") }
                        Button { showSettings = true } label: { Label("Settings", systemImage: "gearshape") }
                        Button { Task { await reconnect() } } label: { Label("Reconnect", systemImage: "arrow.clockwise") }
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
        .fullScreenCover(isPresented: $showGuest, onDismiss: { liveReload += 1 }) { GuestView() }
        .sheet(isPresented: $showGallery, onDismiss: { liveReload += 1; Task { await loadThumb() } }) { GalleryView() }
        .sheet(isPresented: $showAdmin, onDismiss: { liveReload += 1 }) { AdminView() }
        .sheet(isPresented: $showSettings, onDismiss: { liveReload += 1 }) { SettingsSheet() }
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
        let ok = booth.status != nil
        let streaming = booth.status?.camera_stream?.streaming ?? false
        let text = booth.status == nil ? "No booth" : (streaming ? "Live" : "Camera offline")
        return Label(text, systemImage: ok && streaming ? "dot.radiowaves.left.and.right" : "exclamationmark.triangle")
            .font(.caption.bold()).foregroundStyle(.white)
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background((ok && streaming ? Color.green : Color.red).opacity(0.85))
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
        await booth.refresh()
        await loadThumb()
        triggering = false
    }
    private func reconnect() async { await booth.connectAndRefresh() }
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
