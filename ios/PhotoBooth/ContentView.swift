import SwiftUI

struct ContentView: View {
    @EnvironmentObject var booth: BoothClient
    var body: some View {
        BoothView()
            .task { await booth.connectAndRefresh(); await booth.checkAuth() }
    }
}

/// Booth address + auto-join Wi-Fi settings.
struct SettingsSheet: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) var dismiss
    @State private var text = ""
    @AppStorage("wifiAuto") private var wifiAuto = true
    @AppStorage("wifiSSID") private var wifiSSID = "PhotoBooth"
    @AppStorage("wifiPass") private var wifiPass = "booth1234"

    var body: some View {
        NavigationStack {
            Form {
                Section("Booth address") {
                    TextField("http://192.168.50.1:8000", text: $text)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .keyboardType(.URL)
                }
                Section("Booth Wi-Fi") {
                    Toggle("Auto-join on open", isOn: $wifiAuto)
                    TextField("Network name (SSID)", text: $wifiSSID)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("Password", text: $wifiPass)
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
}
