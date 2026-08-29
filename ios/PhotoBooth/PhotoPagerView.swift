import SwiftUI
import UIKit

/// Full-screen photo viewer: swipe between photos, pinch (or double-tap) to zoom,
/// select/unselect the current photo, close back to the tiles. Used from the gallery
/// and the guest results grid.
struct PhotoPagerView: View {
    @EnvironmentObject var booth: BoothClient
    @Environment(\.dismiss) private var dismiss
    let photos: [String]
    @Binding var selected: Set<String>
    @State private var index: Int
    @State private var busy = false
    @State private var share: TempFiles?

    init(photos: [String], selected: Binding<Set<String>>, start: Int) {
        self.photos = photos
        self._selected = selected
        self._index = State(initialValue: min(max(0, start), max(0, photos.count - 1)))
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            TabView(selection: $index) {
                ForEach(Array(photos.enumerated()), id: \.offset) { i, u in
                    ZoomableAsyncImage(url: booth.url(u)).tag(i).ignoresSafeArea()
                }
            }
            .tabViewStyle(.page(indexDisplayMode: .never))
            .ignoresSafeArea()

            VStack {
                HStack {
                    Button { dismiss() } label: { chrome("xmark") }
                    Spacer()
                    Text("\(index + 1) of \(photos.count)")
                        .font(.headline).foregroundStyle(.white)
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(.black.opacity(0.45)).clipShape(Capsule())
                    Spacer()
                    Button { toggleCurrent() } label: {
                        chrome(isSelected ? "checkmark.circle.fill" : "circle",
                               tint: isSelected ? .green : .white)
                    }
                }
                .padding()
                Spacer()
                // Act on the photo you are actually looking at. Without these the only
                // way to send one photo was to close the viewer, find its tile again
                // and select it.
                HStack(spacing: 18) {
                    Button { Task { await airdropCurrent() } } label: { chrome("square.and.arrow.up") }
                    Button { downloadCurrent() } label: { chrome("square.and.arrow.down") }
                }
                .disabled(busy)
                .padding(.bottom, 24)
                .overlay(alignment: .top) {
                    if busy { ProgressView().tint(.white).offset(y: -22) }
                }
            }
        }
        .sheet(item: $share) { s in ShareSheet(items: s.urls) }
        .idleReturn()
    }

    private var current: String? {
        photos.indices.contains(index) ? photos[index] : nil
    }

    private func airdropCurrent() async {
        guard let u = current else { return }
        busy = true
        let files = await booth.downloadToTemp([u])
        busy = false
        if !files.isEmpty { share = TempFiles(urls: files) }
    }

    private func downloadCurrent() {
        guard let u = current else { return }
        let q = u.addingPercentEncoding(withAllowedCharacters: .urlQueryValueAllowed) ?? u
        UIApplication.shared.open(booth.url("/api/download?p=" + q))
    }

    private var isSelected: Bool {
        photos.indices.contains(index) && selected.contains(photos[index])
    }
    private func toggleCurrent() {
        guard photos.indices.contains(index) else { return }
        let u = photos[index]
        if selected.contains(u) { selected.remove(u) } else { selected.insert(u) }
    }
    private func chrome(_ icon: String, tint: Color = .white) -> some View {
        Image(systemName: icon).font(.title2.bold()).foregroundStyle(tint)
            .frame(width: 46, height: 46).background(.black.opacity(0.45)).clipShape(Circle())
    }
}

/// Native pinch-zoom + pan via UIScrollView (SwiftUI has no built-in equivalent with
/// proper zoom physics). Double-tap toggles 1x / 2.5x. Loads the full-resolution photo.
struct ZoomableAsyncImage: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> UIScrollView {
        let scroll = UIScrollView()
        scroll.minimumZoomScale = 1
        scroll.maximumZoomScale = 5
        scroll.delegate = context.coordinator
        scroll.showsVerticalScrollIndicator = false
        scroll.showsHorizontalScrollIndicator = false
        scroll.backgroundColor = .black
        scroll.contentInsetAdjustmentBehavior = .never

        let iv = UIImageView(frame: scroll.bounds)
        iv.contentMode = .scaleAspectFit
        iv.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        scroll.addSubview(iv)
        context.coordinator.imageView = iv

        let doubleTap = UITapGestureRecognizer(target: context.coordinator,
                                               action: #selector(Coordinator.doubleTap(_:)))
        doubleTap.numberOfTapsRequired = 2
        scroll.addGestureRecognizer(doubleTap)

        context.coordinator.load(url)
        return scroll
    }

    func updateUIView(_ scroll: UIScrollView, context: Context) {
        if context.coordinator.url != url {
            scroll.setZoomScale(1, animated: false)
            context.coordinator.load(url)
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        var imageView: UIImageView?
        var url: URL?

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

        func load(_ u: URL) {
            url = u
            Task { @MainActor in
                var req = URLRequest(url: u)
                req.cachePolicy = .returnCacheDataElseLoad
                if let (data, _) = try? await URLSession.shared.data(for: req) {
                    // ignore if the page was recycled for a different photo meanwhile
                    if self.url == u { self.imageView?.image = UIImage(data: data) }
                }
            }
        }

        @objc func doubleTap(_ g: UITapGestureRecognizer) {
            guard let scroll = g.view as? UIScrollView else { return }
            scroll.setZoomScale(scroll.zoomScale > 1 ? 1 : 2.5, animated: true)
        }
    }
}
