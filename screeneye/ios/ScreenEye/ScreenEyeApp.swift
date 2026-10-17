import SwiftUI
import AVFoundation

@main
struct ScreenEyeApp: App {
    @State private var camera = Camera()
    var body: some Scene {
        WindowGroup {
            ContentView(camera: camera)
                .onAppear {
                    UIApplication.shared.isIdleTimerDisabled = true
                    camera.start()
                }
        }
    }
}

/// The live preview, zoomed and panned to the viewport (the lens's frame is what the preview
/// layer shows; the digital crop and pan are a transform of it).
struct PreviewView: UIViewRepresentable {
    let session: AVCaptureSession
    let viewport: Viewport
    /// The raw frame's width / height.
    var aspect: Double = 16.0 / 9.0
    /// Told the preview's clockwise turn of the raw frame when it changes.
    var onAngle: (Int) -> Void = { _ in }

    func makeUIView(context: Context) -> PreviewUIView {
        let v = PreviewUIView()
        v.previewLayer.session = session
        v.previewLayer.videoGravity = .resizeAspect
        v.onAngle = onAngle
        return v
    }

    func updateUIView(_ uiView: PreviewUIView, context: Context) {
        uiView.onAngle = onAngle
        if uiView.viewport != viewport || uiView.aspect != aspect {
            uiView.viewport = viewport
            uiView.aspect = aspect
            uiView.setNeedsLayout()
        }
    }
}

final class PreviewUIView: UIView {
    let previewLayer = AVCaptureVideoPreviewLayer()
    var viewport = Viewport()
    var aspect = 16.0 / 9.0
    var onAngle: (Int) -> Void = { _ in }
    private var angle = -1

    override init(frame: CGRect) {
        super.init(frame: frame)
        clipsToBounds = true
        layer.addSublayer(previewLayer)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    // Keep the on-phone preview upright as the interface rotates (back camera angles), and
    // fill it with the viewport. Snapshots are unaffected: they're cropped from the sensor's
    // frame, in its native landscape.
    override func layoutSubviews() {
        super.layoutSubviews()
        let a = ViewportMath.previewAngle(window?.windowScene?.interfaceOrientation)
        if let c = previewLayer.connection, c.isVideoRotationAngleSupported(CGFloat(a)) {
            c.videoRotationAngle = CGFloat(a)
        }
        if a != angle {
            angle = a
            let report = onAngle
            DispatchQueue.main.async { report(a) }
        }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        previewLayer.setAffineTransform(.identity)
        previewLayer.bounds = CGRect(origin: .zero, size: bounds.size)
        previewLayer.position = CGPoint(x: bounds.midX, y: bounds.midY)
        let shown = ViewportMath.fitted(aspect: ViewportMath.shownAspect(raw: aspect, angle: a), in: bounds.size)
        previewLayer.setAffineTransform(ViewportMath.previewTransform(viewport, angle: a, shown: shown))
        CATransaction.commit()
    }
}
