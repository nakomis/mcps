import SwiftUI
import AVFoundation

@main
struct ScreenEyeApp: App {
    @State private var camera = Camera()
    var body: some Scene {
        WindowGroup {
            ZStack(alignment: .bottomLeading) {
                PreviewView(session: camera.session).ignoresSafeArea()
                Text(camera.statusLine)
                    .font(.system(.caption, design: .monospaced))
                    .padding(6).background(.black.opacity(0.6)).foregroundStyle(.white)
            }
            .onAppear {
                UIApplication.shared.isIdleTimerDisabled = true
                camera.start()
            }
        }
    }
}

struct PreviewView: UIViewRepresentable {
    let session: AVCaptureSession
    func makeUIView(context: Context) -> PreviewUIView {
        let v = PreviewUIView()
        v.previewLayer.session = session
        v.previewLayer.videoGravity = .resizeAspect
        return v
    }
    func updateUIView(_ uiView: PreviewUIView, context: Context) {}
}

final class PreviewUIView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
    var previewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }

    // Keep the on-phone preview upright as the interface rotates (back camera angles).
    // Snapshots are unaffected: they come straight from the sensor's native landscape.
    override func layoutSubviews() {
        super.layoutSubviews()
        let angle: CGFloat = switch window?.windowScene?.interfaceOrientation {
        case .landscapeRight: 0
        case .landscapeLeft: 180
        case .portraitUpsideDown: 270
        default: 90
        }
        if let c = previewLayer.connection, c.isVideoRotationAngleSupported(angle) {
            c.videoRotationAngle = angle
        }
    }
}
