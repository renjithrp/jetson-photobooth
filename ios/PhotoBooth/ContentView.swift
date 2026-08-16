import SwiftUI

struct ContentView: View {
    @EnvironmentObject var booth: BoothClient
    @State private var showSettings = false

    var body: some View {
        TabView {
            CaptureTab().tabItem { Label("Booth", systemImage: "camera.viewfinder") }
            FindPhotosTab().tabItem { Label("My Photos", systemImage: "person.crop.square") }
            AdminTab().tabItem { Label("Admin", systemImage: "lock.shield") }
        }
        .task { await booth.connectAndRefresh(); await booth.checkAuth() }
        .overlay(alignment: .top) { connectionBanner }
    }

    @ViewBuilder private var connectionBanner: some View {
        if booth.status == nil {
            Button { showSettings = true } label: {
                Label("Tap to set booth address (\(booth.baseURL))", systemImage: "wifi.exclamationmark")
                    .font(.footnote).padding(8).background(.yellow.opacity(0.9))
                    .clipShape(Capsule())
            }
            .sheet(isPresented: $showSettings) { SettingsSheet() }
        }
    }
}

/// Lets the operator point the app at the booth (host on the guest hotspot).
struct SettingsSheet: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) var dismiss
    @State private var text = ""
    // Same UserDefaults keys BoothClient reads, so edits here are picked up on next connect.
    @AppStorage("wifiAuto") private var wifiAuto = true
    @AppStorage("wifiSSID") private var wifiSSID = "PhotoBooth"
    @AppStorage("wifiPass") private var wifiPass = "booth1234"
    var body: some View {
        NavigationStack {
            Form {
                Section("Booth address") {
                    TextField("http://192.168.50.1", text: $text)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .keyboardType(.URL)
                }
                Section("Booth Wi-Fi") {
                    Toggle("Auto-join on open", isOn: $wifiAuto)
                    TextField("Network name (SSID)", text: $wifiSSID)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("Password", text: $wifiPass)
                }
                Section { Text("Use the booth's hotspot IP (default http://192.168.50.1) or its LAN address on :8000. Auto-join connects this device to the booth hotspot when the app opens (iOS asks once to allow it).").font(.footnote) }
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
