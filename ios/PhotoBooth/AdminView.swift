import SwiftUI

/// PIN-gated admin sheet: the WhatsApp send queue (send + mark sent).
struct AdminView: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) var dismiss
    @Environment(\.openURL) var openURL
    @State private var pin = ""
    @State private var pending: [PendingRecipient] = []
    @State private var loading = false

    var body: some View {
        NavigationStack {
            Group { if booth.isAdmin { console } else { gate } }
                .navigationTitle("Admin")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .topBarLeading) { Button("Close") { dismiss() } } }
        }
    }

    private var gate: some View {
        VStack(spacing: 16) {
            Image(systemName: "lock.shield").font(.largeTitle)
            SecureField("PIN", text: $pin).keyboardType(.numberPad)
                .multilineTextAlignment(.center).font(.title2)
                .frame(width: 160).textFieldStyle(.roundedBorder)
            Button("Unlock") { Task { _ = await booth.login(pin: pin); await reload() } }
                .buttonStyle(.borderedProminent)
        }.padding()
    }

    private var console: some View {
        List {
            Section("WhatsApp send queue") {
                if pending.isEmpty {
                    Text(loading ? "Loading…" : "No one waiting to be sent.").foregroundStyle(.secondary)
                }
                ForEach(pending) { r in
                    VStack(alignment: .leading, spacing: 8) {
                        Text("+\(r.phone) · \(r.count) photo\(r.count > 1 ? "s" : "")").font(.headline)
                        HStack {
                            Button { if let u = URL(string: r.wa_link) { openURL(u) } } label: {
                                Label("Open WhatsApp", systemImage: "message.fill")
                            }.buttonStyle(.bordered)
                            Button(role: .destructive) {
                                Task { _ = await booth.markSent(phone: r.phone); await reload() }
                            } label: { Label("Mark sent", systemImage: "checkmark") }.buttonStyle(.bordered)
                        }
                    }.padding(.vertical, 4)
                }
            }
            Section { Button("Log out") { booth.isAdmin = false } }
        }
        .refreshable { await reload() }
        .task { await reload() }
    }

    private func reload() async { loading = true; pending = await booth.whatsappPending(); loading = false }
}
