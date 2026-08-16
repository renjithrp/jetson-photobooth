import SwiftUI

/// Kiosk idle timeout: after 10s without a touch, shows a cancellable countdown
/// ("going back to the camera in 5…"), then dismisses back to the live view.
/// Any touch — or the "I'm still here" button — cancels and restarts the clock.
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
    @StateObject private var idle = IdleMonitor()
    @Environment(\.dismiss) private var dismiss

    func body(content: Content) -> some View {
        content
            // A zero-distance drag gesture can steal taps from toolbar/nav buttons, so
            // detect activity with a plain tap plus a real-threshold drag (scrolling).
            .simultaneousGesture(TapGesture().onEnded { idle.touch() })
            .simultaneousGesture(DragGesture(minimumDistance: 12).onChanged { _ in idle.touch() })
            .overlay {
                if idle.counting {
                    VStack(spacing: 18) {
                        Image(systemName: "camera.viewfinder").font(.largeTitle)
                        Text("Going back to the camera in \(idle.secondsLeft)…")
                            .font(.title2.bold()).multilineTextAlignment(.center)
                        Button { idle.touch() } label: {
                            Text("I'm still here").font(.title3.bold()).padding(.horizontal, 12)
                        }
                        .buttonStyle(.borderedProminent).controlSize(.large)
                    }
                    .padding(30)
                    .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 24))
                    .shadow(radius: 20)
                    .transition(.opacity)
                }
            }
            .onAppear { idle.onTimeout = { dismiss() }; idle.touch() }
            .onDisappear { idle.stop() }
    }
}

extension View {
    /// Apply to guest-facing screens so an abandoned session returns to the live view.
    func idleReturn() -> some View { modifier(IdleReturn()) }
}
