import SwiftUI

extension Notification.Name {
    /// Any user activity (taps, keypad presses) — resets every idle timer on screen.
    static let boothActivity = Notification.Name("boothActivity")
    /// Unwind everything back to the live camera view.
    static let boothGoHome = Notification.Name("boothGoHome")
}

/// Kiosk idle timeout: after 10s without a touch/keystroke, shows a countdown card
/// with a mini live view — "I'm still here" (or any touch) cancels; "Go to camera"
/// (or the countdown ending) unwinds every open screen back to the booth camera.
@MainActor
final class IdleMonitor: ObservableObject {
    @Published var counting = false
    @Published var secondsLeft = 5
    var onTimeout: (() -> Void)?
    private var task: Task<Void, Never>?

    static let idleSeconds: Double = 10
    static let countdownSeconds = 5

    func touch() {
        task?.cancel()
        counting = false
        task = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.idleSeconds))
            guard !Task.isCancelled, let self else { return }
            self.counting = true
            for s in stride(from: Self.countdownSeconds, through: 1, by: -1) {
                self.secondsLeft = s
                try? await Task.sleep(for: .seconds(1))
                if Task.isCancelled { return }
            }
            if self.counting { self.onTimeout?() }
        }
    }

    func stop() { task?.cancel(); counting = false }
}

struct IdleReturn: ViewModifier {
    /// Pause while a child screen (sheet/cover) is presented on top — the child runs
    /// its own idle timer, and the parent must not count down underneath it.
    var paused = false
    @StateObject private var idle = IdleMonitor()
    @EnvironmentObject private var booth: BoothClient
    @Environment(\.dismiss) private var dismiss

    func body(content: Content) -> some View {
        content
            // taps + real drags (never steals button taps); keypads post .boothActivity
            .simultaneousGesture(TapGesture().onEnded { idle.touch() })
            .simultaneousGesture(DragGesture(minimumDistance: 12).onChanged { _ in idle.touch() })
            .onReceive(NotificationCenter.default.publisher(for: .boothActivity)) { _ in
                if !paused { idle.touch() }
            }
            .onReceive(NotificationCenter.default.publisher(for: .boothGoHome)) { _ in
                idle.stop(); dismiss()
            }
            .overlay { if idle.counting && !paused { countdownCard } }
            .onChange(of: paused) { p in p ? idle.stop() : idle.touch() }
            .onAppear {
                idle.onTimeout = { NotificationCenter.default.post(name: .boothGoHome, object: nil) }
                if !paused { idle.touch() }
            }
            .onDisappear { idle.stop() }
    }

    private var countdownCard: some View {
        VStack(spacing: 14) {
            LiveView(url: booth.url("/api/preview/stream"))
                .frame(width: 300, height: 190)
                .clipShape(RoundedRectangle(cornerRadius: 14))
            Text("Going back to the camera in \(idle.secondsLeft)…")
                .font(.title3.bold()).multilineTextAlignment(.center)
            HStack(spacing: 12) {
                Button { idle.touch() } label: {
                    Text("I'm still here").font(.headline).padding(.horizontal, 6)
                }.buttonStyle(.bordered).controlSize(.large)
                Button { NotificationCenter.default.post(name: .boothGoHome, object: nil) } label: {
                    Label("Go to camera", systemImage: "camera.fill").font(.headline).padding(.horizontal, 6)
                }.buttonStyle(.borderedProminent).controlSize(.large)
            }
        }
        .padding(26)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 24))
        .shadow(radius: 20)
        .transition(.opacity)
    }
}

extension View {
    /// Apply to guest-facing screens; pass `paused: true` while a child sheet is up.
    func idleReturn(paused: Bool = false) -> some View { modifier(IdleReturn(paused: paused)) }
}
