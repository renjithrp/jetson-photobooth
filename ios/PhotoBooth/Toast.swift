import SwiftUI

/// A short, self-dismissing status message.
///
/// The booth is used at arm's length by staff and guests, so feedback has to be
/// impossible to miss: the gallery used to report "Saved to Drive" as grey
/// footnote text at the top of the sheet, which reads as decoration and never
/// cleared itself.
struct ToastMessage: Equatable, Identifiable {
    enum Kind { case info, success, error }

    let id = UUID()
    var text: String
    var kind: Kind = .info

    var icon: String {
        switch kind {
        case .info:    return "info.circle.fill"
        case .success: return "checkmark.circle.fill"
        case .error:   return "exclamationmark.triangle.fill"
        }
    }
    var tint: Color {
        switch kind {
        case .info:    return .accentColor
        case .success: return .green
        case .error:   return .red
        }
    }
    /// Errors linger — they usually ask the reader to do something.
    var seconds: Double { kind == .error ? 5 : 2.5 }
}

extension View {
    func toast(_ message: Binding<ToastMessage?>) -> some View {
        modifier(ToastModifier(message: message))
    }
}

private struct ToastModifier: ViewModifier {
    @Binding var message: ToastMessage?

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .top) {
                if let m = message {
                    HStack(spacing: 10) {
                        Image(systemName: m.icon).foregroundStyle(m.tint)
                        Text(m.text).font(.subheadline.weight(.medium))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.horizontal, 16).padding(.vertical, 12)
                    .background(.regularMaterial, in: Capsule())
                    .shadow(radius: 8, y: 4)
                    .padding(.top, 8)
                    .transition(.move(edge: .top).combined(with: .opacity))
                    // keyed on id so a replacement message restarts the timer
                    .task(id: m.id) {
                        try? await Task.sleep(for: .seconds(m.seconds))
                        guard !Task.isCancelled else { return }
                        withAnimation { if message?.id == m.id { message = nil } }
                    }
                }
            }
            .animation(.easeInOut(duration: 0.25), value: message)
    }
}
