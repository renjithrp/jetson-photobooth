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
        // The stream is display-only; let touches fall through to SwiftUI so the
        // booth screen's tap-to-focus gesture works (WKWebView eats them otherwise).
        web.isUserInteractionEnabled = false
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
            // The <img> self-heals: a booth backend restart ends the MJPEG stream
            // silently (fires `load`, not `error`, for multipart streams) and nothing
            // else revives it — the live view froze while the status pill stayed
            // green. Debounced reconnect on both events, with a cache-buster.
            let html = """
            <html><body style="margin:0;background:#000;height:100vh;display:flex;
            align-items:center;justify-content:center;overflow:hidden">
            <img id="v" src="\(url.absoluteString)" style="max-width:100%;max-height:100%;transform:scaleX(-1)">
            <script>
            // WebKit fires `load` repeatedly while an x-mixed-replace stream runs, so
            // treat load events as a HEARTBEAT: reconnect only when they stop (stream
            // ended, e.g. booth restart) or on error. Reconnecting on every load — the
            // Chromium-style fix — cycled a healthy stream every ~3s here.
            const v = document.getElementById('v');
            const base = '\(url.absoluteString)';
            let beat = Date.now();
            v.onload  = () => { beat = Date.now(); };
            v.onerror = () => { beat = 0; };
            setInterval(() => {
              if (Date.now() - beat > 9000) {
                beat = Date.now();   // one reconnect per stall window
                v.src = base + (base.includes('?') ? '&' : '?') + 't=' + Date.now();
              }
            }, 3000);
            </script>
            </body></html>
            """
            web.loadHTMLString(html, baseURL: url)
        }
    }
}
