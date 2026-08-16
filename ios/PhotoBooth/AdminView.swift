import SwiftUI

/// PIN-gated admin sheet: booth status, service restarts, upstream Wi-Fi,
/// and the WhatsApp send queue (send + mark sent).
struct AdminView: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) var dismiss
    @Environment(\.openURL) var openURL
    @State private var pin = ""
    @State private var pending: [PendingRecipient] = []
    @State private var loading = false
    @State private var net: NetworkStatus?
    @State private var services: [String: String] = [:]
    @State private var actionMsg: String?
    @State private var restarting: String?           // service currently restarting
    @State private var nets: [WifiNet] = []
    @State private var scanning = false
    @State private var joinTarget: WifiNet?          // network awaiting a password
    @State private var joinPassword = ""

    /// Display order + friendly names for the controllable units.
    private let serviceNames: [(String, String)] = [
        ("photobooth", "Backend"), ("photobooth-camera", "Camera daemon"),
        ("photobooth-gesture", "Gesture worker"), ("photobooth-captive", "Captive portal"),
    ]

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
            statusSection
            servicesSection
            wifiSection
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

    // MARK: - status
    private var statusSection: some View {
        Section("Status") {
            let cs = booth.status?.camera_stream
            row("Camera", cs?.streaming == true ? "Live · \(Int(cs?.fps ?? 0)) fps"
                : cs?.recovering == true ? "Reconnecting…" : "Offline",
                ok: cs?.streaming == true)
            row("Session", booth.status?.busy == true ? "Capturing…" : "Idle",
                ok: booth.status?.busy != true)
            row("Internet", (net?.internet ?? "…") +
                ((net?.mgmt_ssid).map { $0.isEmpty ? "" : " · \($0)" } ?? ""),
                ok: net?.internet == "full")
            row("Guest hotspot", net?.hotspot?.active == true
                ? (net?.hotspot?.ssid ?? "on") : "off",
                ok: net?.hotspot?.active == true)
        }
    }

    private func row(_ name: String, _ value: String, ok: Bool) -> some View {
        HStack {
            Circle().fill(ok ? .green : .orange).frame(width: 9, height: 9)
            Text(name)
            Spacer()
            Text(value).foregroundStyle(.secondary)
        }
    }

    // MARK: - services
    private var servicesSection: some View {
        Section("Services") {
            ForEach(serviceNames, id: \.0) { (svc, label) in
                HStack {
                    Circle().fill(services[svc] == "active" ? .green : .red)
                        .frame(width: 9, height: 9)
                    Text(label)
                    Spacer()
                    Text(services[svc] ?? "…").foregroundStyle(.secondary).font(.footnote)
                    Button(restarting == svc ? "Restarting…" : "Restart") {
                        Task { await restart(svc) }
                    }
                    .buttonStyle(.bordered).font(.footnote)
                    .disabled(restarting != nil)
                }
            }
            if let m = actionMsg { Text(m).font(.footnote).foregroundStyle(.secondary) }
        }
    }

    private func restart(_ svc: String) async {
        restarting = svc
        actionMsg = nil
        let r = await booth.serviceAction(svc, "restart")
        if svc == "photobooth" {
            // backend restart detaches and drops our connection — give it time to come back
            actionMsg = "Backend restarting… reconnecting"
            try? await Task.sleep(for: .seconds(6))
        } else if r.ok {
            actionMsg = "\(svc) restarted"
            try? await Task.sleep(for: .seconds(2))
        } else {
            actionMsg = "\(svc): \(r.error ?? "restart failed")"
        }
        await booth.refresh()
        await reloadSystem()
        restarting = nil
    }

    // MARK: - booth upstream wi-fi
    private var wifiSection: some View {
        Section("Booth internet (Wi-Fi)") {
            Button {
                Task { scanning = true; nets = await booth.wifiScan(); scanning = false }
            } label: { Label(scanning ? "Scanning…" : "Scan networks", systemImage: "wifi") }
            .disabled(scanning)
            ForEach(nets) { n in
                HStack {
                    Image(systemName: n.in_use == true ? "checkmark.circle.fill" : "wifi")
                        .foregroundStyle(n.in_use == true ? .green : .secondary)
                    Text(n.ssid)
                    if (n.security ?? "open") != "open" {
                        Image(systemName: "lock.fill").font(.caption2).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text("\(n.signal ?? 0)%").font(.footnote).foregroundStyle(.secondary)
                    if n.in_use == true {
                        Button("Forget", role: .destructive) {
                            Task { _ = await booth.wifiForget(ssid: n.ssid); await reloadSystem()
                                   nets = await booth.wifiScan() }
                        }.buttonStyle(.bordered).font(.footnote)
                    } else {
                        Button("Join") {
                            if (n.security ?? "open") == "open" {
                                Task { await join(n, password: "") }
                            } else { joinPassword = ""; joinTarget = n }
                        }.buttonStyle(.bordered).font(.footnote)
                    }
                }
            }
        }
        .alert("Join \(joinTarget?.ssid ?? "")", isPresented: .init(
            get: { joinTarget != nil }, set: { if !$0 { joinTarget = nil } })) {
            SecureField("Password", text: $joinPassword)
            Button("Join") { if let t = joinTarget { Task { await join(t, password: joinPassword) } } }
            Button("Cancel", role: .cancel) {}
        }
    }

    private func join(_ n: WifiNet, password: String) async {
        actionMsg = "Joining \(n.ssid)…"
        let r = await booth.wifiConnect(ssid: n.ssid, password: password)
        actionMsg = r.ok ? "Joined \(n.ssid)" : "Join failed: \(r.error ?? "?")"
        await reloadSystem()
        nets = await booth.wifiScan()
    }

    // MARK: - loads
    private func reloadSystem() async {
        net = await booth.networkStatus()
        services = await booth.serviceStates()
    }

    private func reload() async {
        loading = true
        pending = await booth.whatsappPending()
        loading = false
        await reloadSystem()
    }
}
