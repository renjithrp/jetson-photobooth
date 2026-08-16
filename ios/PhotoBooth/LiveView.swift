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
            <canvas id="g" style="position:fixed;inset:0;pointer-events:none"></canvas>
            <div id="s" style="display:none;position:fixed;left:10px;top:10px;
              font:600 12px/1.5 ui-monospace,Menlo,monospace;background:#000b;color:#a7f3d0;
              padding:8px 12px;border-radius:10px;white-space:pre;pointer-events:none"></div>
            <script>
            // Gesture debug overlay (mirrors the kiosk): when the admin setting
            // trigger.show_gesture_overlay is on, poll the worker's verdict and draw
            // the hand skeleton + reason over the (mirrored, contain-fit) stream.
            const gc = document.getElementById('g'), gx = gc.getContext('2d');
            const BONES = [[0,1],[1,2],[2,3],[3,4],[0,5],[5,6],[6,7],[7,8],[5,9],[9,10],
              [10,11],[11,12],[9,13],[13,14],[14,15],[15,16],[13,17],[17,18],[18,19],[19,20],[0,17]];
            let overlayOn = false, statsOn = false;
            const sd = document.getElementById('s');
            async function pollCfg(){
              try { const s = await (await fetch('/api/settings')).json();
                    overlayOn = !!(s.trigger && s.trigger.show_gesture_overlay);
                    statsOn = !!(s.trigger && s.trigger.show_gesture_stats);
                    if(!overlayOn) gx.clearRect(0,0,gc.width,gc.height);
                    if(!statsOn) sd.style.display = 'none';
              } catch(e){} }
            pollCfg(); setInterval(pollCfg, 5000);
            function stats(ev){
              if (!statsOn){ sd.style.display = 'none'; return; }
              sd.style.display = 'block';
              const pct = v => v == null ? '-' : Math.round(v*100) + '%';
              const yn = v => v === true ? 'Y' : v === false ? 'N' : '-';
              sd.textContent = !ev.hand ? ('no hand' + (ev.tune_mode ? '  [TUNE MODE]' : '')) :
                'want ' + ev.want + '  hands ' + (ev.hands ?? 1) + '  score ' + (ev.score ?? '-') +
                (ev.tune_mode ? '  [TUNE MODE]' : '') + '\\n' +
                'span ' + pct(ev.span) + '  min ' + pct(ev.min_size) + '  face_h ' + (ev.face_h ? pct(ev.face_h) : '-') + '  zoom ' + (ev.zoom ? 'Y' : '-') + '\\n' +
                'pose ' + yn(ev.match) + '  palm ' + yn(ev.palm) + '  size ' + yn(ev.size_ok) + '  in_frame ' + yn(ev.in_frame) +
                '  near_face ' + yn(ev.near_face) + '  on_face ' + (ev.on_face ? 'YES' : 'no') + '\\n' +
                'streak ' + (ev.streak ?? 0) + '  hold ' + pct(ev.hold_progress) +
                '  ratio ' + pct(ev.hold_ratio) + '  cooldown ' + (ev.cooldown_left ?? 0) + 's';
            }
            setInterval(async () => {
              if (!overlayOn && !statsOn) return;
              let ev; try { ev = await (await fetch('/api/gesture/state')).json(); } catch(e){ return; }
              stats(ev);
              if (!overlayOn) return;
              if (gc.width !== innerWidth || gc.height !== innerHeight){ gc.width = innerWidth; gc.height = innerHeight; }
              gx.clearRect(0,0,gc.width,gc.height);
              if (!ev.hand || !ev.lm || (Date.now()/1000 - (ev.t||0)) > 1.5) return;
              const el = document.getElementById('v');
              const iw = el.naturalWidth||4, ih = el.naturalHeight||3;
              const sc = Math.min(gc.width/iw, gc.height/ih);       // contain fit
              const dw = iw*sc, dh = ih*sc, ox = (gc.width-dw)/2, oy = (gc.height-dh)/2;
              const P = p => [ox + (1-p[0])*dw, oy + p[1]*dh];      // stream is mirrored
              const tooSmall = ev.size_ok === false;
              const col = ev.on_face ? '#f87171'
                : (ev.match && !tooSmall && ev.near_face !== false) ? '#4ade80' : '#fbbf24';
              gx.strokeStyle = col; gx.fillStyle = col; gx.lineWidth = 3; gx.lineCap = 'round';
              for (const [a,b] of BONES){ const [x1,y1]=P(ev.lm[a]), [x2,y2]=P(ev.lm[b]);
                gx.beginPath(); gx.moveTo(x1,y1); gx.lineTo(x2,y2); gx.stroke(); }
              for (const p of ev.lm){ const [x,y]=P(p); gx.beginPath(); gx.arc(x,y,4,0,7); gx.fill(); }
              const [wx,wy] = P(ev.lm[0]);
              const reason = (ev.tune_mode && ev.would_fire) ? 'WOULD FIRE \\u2713 (tune mode)'
                : ev.on_face ? 'rejected: looks like a face'
                : tooSmall ? ('too small/far (' + Math.round(ev.span*100) + '% < ' + Math.round(ev.min_size*100) + '%) \\u2014 pose ignored')
                : ev.near_face === false ? 'no face near the hand'
                : !ev.in_frame ? 'hand not fully in frame'
                : (ev.palm === false && ev.want === 'open_palm') ? 'back of hand \\u2014 show your palm'
                : !ev.match ? ('pose \\u2260 ' + ev.want)
                : ev.cooldown_left > 0 ? ('cooldown ' + ev.cooldown_left + 's')
                : ev.want === 'wave' ? ('wave ' + (ev.swings||0) + '/3 swings')
                : ev.hold_progress > 0 ? ('hold ' + (ev.hold_progress*ev.hold_need).toFixed(1) + '/' + ev.hold_need + 's')
                : ev.confirming ? 'confirming\\u2026'
                : ev.want + ' \\u2713';
              gx.font = '700 15px -apple-system'; gx.textAlign = 'center';
              const tw = gx.measureText(reason).width + 20;
              const bx = Math.min(Math.max(wx, tw/2+6), gc.width-tw/2-6), by = Math.max(wy-50, 26);
              gx.fillStyle = '#000a'; gx.beginPath(); gx.roundRect(bx-tw/2, by-17, tw, 25, 12); gx.fill();
              gx.fillStyle = col; gx.fillText(reason, bx, by+1);
              if (ev.hold_progress > 0){
                gx.strokeStyle = '#ffffff55'; gx.lineWidth = 5;
                gx.beginPath(); gx.arc(wx, wy, 26, 0, 2*Math.PI); gx.stroke();
                gx.strokeStyle = col;
                gx.beginPath(); gx.arc(wx, wy, 26, -Math.PI/2, -Math.PI/2 + 2*Math.PI*ev.hold_progress); gx.stroke();
              }
            }, 200);
            </script>
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
