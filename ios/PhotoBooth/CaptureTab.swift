import SwiftUI

/// Live view + trigger. Mirrors the web /control page.
struct CaptureTab: View {
    @EnvironmentObject var booth: BoothClient
    @State private var triggering = false

    var body: some View {
        VStack(spacing: 0) {
            LiveView(url: booth.url("/api/preview/stream"))
                .background(.black)
                .overlay(alignment: .topLeading) { statusPill.padding() }

            Button {
                Task {
                    triggering = true
                    _ = await booth.trigger()
                    try? await Task.sleep(for: .seconds(1))
                    await booth.refresh()
                    triggering = false
                }
            } label: {
                Label(triggering ? "Starting…" : "Take Photo", systemImage: "camera.fill")
                    .font(.title2.bold()).frame(maxWidth: .infinity).padding()
            }
            .buttonStyle(.borderedProminent)
            .disabled(triggering || (booth.status?.busy ?? false))
            .padding()
        }
    }

    private var statusPill: some View {
        let streaming = booth.status?.camera_stream?.streaming ?? false
        return Label(streaming ? "Live" : "Camera offline",
                     systemImage: streaming ? "dot.radiowaves.left.and.right" : "video.slash")
            .font(.caption.bold()).foregroundStyle(.white)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background((streaming ? Color.green : Color.red).opacity(0.85))
            .clipShape(Capsule())
    }
}
