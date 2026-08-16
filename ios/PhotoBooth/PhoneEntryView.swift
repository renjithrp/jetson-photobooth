import SwiftUI

/// Kiosk-friendly WhatsApp number entry: a dedicated screen with a huge readout and
/// a big on-screen keypad (no fiddly system keyboard). Starts with "+" for the
/// country code; Send enables once the number looks plausible.
struct PhoneEntryView: View {
    @Environment(\.dismiss) private var dismiss
    let photoCount: Int
    let onSubmit: (String) -> Void

    @State private var number = "+"
    private var digitCount: Int { number.filter(\.isNumber).count }
    private var valid: Bool { (7...15).contains(digitCount) }

    private let rows: [[String]] = [["1", "2", "3"], ["4", "5", "6"],
                                    ["7", "8", "9"], ["+", "0", "⌫"]]

    var body: some View {
        NavigationStack {
            VStack(spacing: 22) {
                Text("Your WhatsApp number").font(.largeTitle.bold()).padding(.top, 8)
                Text("Include your country code — we'll send your \(photoCount) photo\(photoCount == 1 ? "" : "s") there.")
                    .font(.title3).foregroundStyle(.secondary)

                Text(number)
                    .font(.system(size: 44, weight: .semibold, design: .monospaced))
                    .frame(maxWidth: 460, minHeight: 64)
                    .padding(.horizontal, 18)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 16))
                    .lineLimit(1).minimumScaleFactor(0.5)

                VStack(spacing: 12) {
                    ForEach(rows, id: \.self) { row in
                        HStack(spacing: 12) {
                            ForEach(row, id: \.self) { key in keyButton(key) }
                        }
                    }
                }

                Button {
                    onSubmit(number); dismiss()
                } label: {
                    Label("Send my photos", systemImage: "message.fill")
                        .font(.title2.bold()).frame(maxWidth: 420).padding(.vertical, 14)
                }
                .buttonStyle(.borderedProminent).controlSize(.large)
                .disabled(!valid)

                Spacer(minLength: 0)
            }
            .padding(28)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Cancel") { dismiss() } }
            }
        }
        .idleReturn()
    }

    private func keyButton(_ key: String) -> some View {
        Button {
            // a keystroke is user activity — keep every idle timer (incl. the parent
            // screen's, paused or not) from popping the go-home countdown mid-typing
            NotificationCenter.default.post(name: .boothActivity, object: nil)
            switch key {
            case "⌫": if number.count > 0 { number.removeLast() }
            case "+": if !number.contains("+") { number = "+" + number }
            default:  if digitCount < 15 { number.append(key) }
            }
        } label: {
            Group {
                if key == "⌫" { Image(systemName: "delete.left") } else { Text(key) }
            }
            .font(.system(size: 30, weight: .semibold))
            .frame(width: 96, height: 68)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
    }
}
