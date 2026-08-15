import SwiftUI

@main
struct PhotoBoothApp: App {
    @StateObject private var booth = BoothClient()
    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(booth)
        }
    }
}
