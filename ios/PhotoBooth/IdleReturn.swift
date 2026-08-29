import SwiftUI

extension Notification.Name {
    /// Any user activity (taps, keypad presses) — resets every idle timer on screen.
    static let boothActivity = Notification.Name("boothActivity")
    /// Unwind everything back to the live camera view.
    static let boothGoHome = Notification.Name("boothGoHome")
}

/// Kiosk idle timeout: after a long stretch with no touch, unwinds every open screen
/// back to the booth camera, silently.
///
/// There used to be a countdown card ("Going back to the camera in 5… / I'm still
/// here"). It is gone: it interrupted guests mid-task to tell them about something
/// that hadn't happened yet, and the choice it offered was one nobody wanted to make.
/// The return itself is kept — a guest's matched photos must not sit on a kiosk
/// screen for the next person — but it now happens quietly, and only after long
/// enough that nobody is still using the booth.
@MainActor
final class IdleMonitor: ObservableObject {
    var onTimeout: (() -> Void)?
    private var task: Task<Void, Never>?

    /// Was 10s + a 5s countdown. With no countdown there is no grace period, so the
    /// wait absorbs it: reading a screen for 90s without a single touch means the
    /// booth has been left.
    static let idleSeconds: Double = 90

    func touch() {
        task?.cancel()
        task = Task { [weak self] in
            try? await Task.sleep(for: .seconds(Self.idleSeconds))
            guard !Task.isCancelled, let self else { return }
            self.onTimeout?()
        }
    }

    func stop() { task?.cancel() }
}

struct IdleReturn: ViewModifier {
    /// Pause while a child screen (sheet/cover) is presented on top — the child runs
    /// its own idle timer, and the parent must not count down underneath it.
    var paused = false
    @StateObject private var idle = IdleMonitor()
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
            .onChange(of: paused) { p in p ? idle.stop() : idle.touch() }
            .onAppear {
                idle.onTimeout = { NotificationCenter.default.post(name: .boothGoHome, object: nil) }
                if !paused { idle.touch() }
            }
            .onDisappear { idle.stop() }
    }
}

extension View {
    /// Apply to guest-facing screens; pass `paused: true` while a child sheet is up.
    func idleReturn(paused: Bool = false) -> some View { modifier(IdleReturn(paused: paused)) }
}
