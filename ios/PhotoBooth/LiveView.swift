import SwiftUI
import WebKit

/// Renders the booth's MJPEG preview (`/api/preview/stream`) in a WKWebView (WebKit
/// decodes multipart/x-mixed-replace natively).
///
/// WebKit suspends the page while it's covered by a sheet, which kills the MJPEG
/// connection — so returning from the gallery/admin left a black frame. `reloadID`
/// lets the parent force a fresh reconnect (it bumps it on sheet-dismiss and when the
/// app returns to the foreground). We load once in makeUIView and only reload when the
/// token changes, so normal SwiftUI updates don't flicker the stream.
struct LiveView: UIViewRepresentable {
    let url: URL
    var reloadID: Int = 0

    func makeUIView(context: Context) -> WKWebView {
        let web = WKWebView()
        web.isOpaque = false
        web.backgroundColor = .black
        web.scrollView.isScrollEnabled = false
        web.scrollView.backgroundColor = .black
        context.coordinator.load(web, url: url, id: reloadID)
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        if context.coordinator.loadedID != reloadID {
            context.coordinator.load(web, url: url, id: reloadID)
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator {
        var loadedID = -1
        func load(_ web: WKWebView, url: URL, id: Int) {
            loadedID = id
            let html = """
            <html><body style="margin:0;background:#000;height:100vh;display:flex;
            align-items:center;justify-content:center;overflow:hidden">
            <img src="\(url.absoluteString)" style="max-width:100%;max-height:100%;transform:scaleX(-1)">
            </body></html>
            """
            web.loadHTMLString(html, baseURL: url)
        }
    }
}
