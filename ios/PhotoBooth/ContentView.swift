import SwiftUI

struct ContentView: View {
    @EnvironmentObject var booth: BoothClient
    var body: some View {
        BoothView()
            .task {
                await booth.connectAndRefresh()
                await booth.checkAuth()
                booth.startWatchdog()      // self-heal dropped Wi-Fi / booth connection
                PhotoSync.shared.start(booth: booth)   // auto-save new photos to Photos
            }
    }
}

/// Booth address + auto-join Wi-Fi settings.
struct SettingsSheet: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) var dismiss
    @State private var text = ""
    @State private var testing = false
    @State private var testResult: (ok: Bool, note: String)?
    @AppStorage("wifiAuto") private var wifiAuto = true
    @AppStorage("wifiSSID") private var wifiSSID = "PhotoBooth"
    @AppStorage("wifiPass") private var wifiPass = "booth1234"
    @AppStorage("autoSyncPhotos") private var autoSync = true

    var body: some View {
        NavigationStack {
            Form {
                // A wrong address here is the app's most common failure and used to
                // be invisible: Save accepted anything and the booth screen just said
                // "No booth". Check it before saving.
                Section("Booth address") {
                    TextField("http://192.168.50.1:8000", text: $text)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .keyboardType(.URL)
                    if text != HOTSPOT_URL {
                        Button("Use booth hotspot address") { text = HOTSPOT_URL; testResult = nil }
                    }
                    Button {
                        Task { await test() }
                    } label: {
                        HStack {
                            Label("Test connection", systemImage: "antenna.radiowaves.left.and.right")
                            Spacer()
                            if testing { ProgressView() }
                        }
                    }
                    .disabled(testing || text.isEmpty)
                    if let r = testResult {
                        Label(r.note, systemImage: r.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .font(.footnote).foregroundStyle(r.ok ? .green : .red)
                    }
                }
                Section("Booth Wi-Fi") {
                    Toggle("Auto-join on open", isOn: $wifiAuto)
                    TextField("Network name (SSID)", text: $wifiSSID)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("Password", text: $wifiPass)
                }
                Section("Photos") {
                    Toggle("Auto-save new photos to this iPad", isOn: $autoSync)
                    Text("New booth photos are saved into the Photos app automatically (checked every 20s). Existing backlogs are skipped.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                Section { Text("Point the app at the booth backend on port 8000 (hotspot: http://192.168.50.1:8000, or the LAN IP). Auto-join connects to the booth hotspot on open (iOS asks once).").font(.footnote) }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { booth.baseURL = text.isEmpty ? booth.baseURL : text
                        Task { await booth.refresh() }; dismiss() }
                }
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            }
            .onAppear { text = booth.baseURL }
        }
    }

    private func test() async {
        testing = true
        let ok = await booth.probe(text)
        testing = false
        testResult = ok
            ? (true, "Booth answered at \(BoothClient.normalized(text)).")
            : (false, "No answer. Check the iPad is on the booth Wi-Fi and the address is right.")
    }
}

/// The booth's own guest AP — the address the iPad uses at an event.
private let HOTSPOT_URL = "http://192.168.50.1:8000"
