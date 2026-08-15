import SwiftUI
import WebKit

/// Renders the booth's MJPEG preview (`/api/preview/stream`). WebKit decodes
/// multipart/x-mixed-replace natively, so a thin WKWebView wrapper is the simplest
/// reliable live view — no hand-rolled MJPEG parser.
struct LiveView: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> WKWebView {
        let web = WKWebView()
        web.isOpaque = false
        web.backgroundColor = .black
        web.scrollView.isScrollEnabled = false
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        // Center the stream on a black page and mirror it (selfie view).
        let html = """
        <html><body style="margin:0;background:#000;height:100vh;display:flex;
        align-items:center;justify-content:center;overflow:hidden">
        <img src="\(url.absoluteString)" style="max-width:100%;max-height:100%;transform:scaleX(-1)">
        </body></html>
        """
        web.loadHTMLString(html, baseURL: url)
    }
}
