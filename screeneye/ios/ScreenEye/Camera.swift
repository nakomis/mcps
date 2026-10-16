import AVFoundation
import CoreImage
import Network
import Observation
import UIKit

/// Runs the back camera with full control and serves it over HTTP on port 8080:
///   GET /snapshot.jpg                    latest frame, raw (sensor landscape); headers
///                                        X-ScreenEye-Orientation and X-ScreenEye-Rotation
///   GET /snapshot.jpg?upright=1          the same frame turned upright
///   GET /set?zoom=4&focus=0.5,0.5&lock=1&ev=-1   zoom (x of the 1x lens), focus point, lock, exposure bias
///   GET /status                          JSON, with `orientation` and `rotation` (clockwise degrees)
@Observable
final class Camera: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    let session = AVCaptureSession()
    var statusLine = "starting…"

    // Zoom state for the on-screen controls (main thread), in multiples of the 1x lens.
    // Follows the device by KVO, so `/set?zoom=` and pinching both move it.
    var zoom: Double = 1
    var minZoom: Double = 1
    var maxZoom: Double = 1

    private var device: AVCaptureDevice?
    private let orientation = OrientationTracker()
    private let output = AVCaptureVideoDataOutput()
    private let queue = DispatchQueue(label: "camera")
    private let lock = NSLock()
    private var latest: CIImage?
    private let ciContext = CIContext()
    private var listener: NWListener?
    private var oneXFactor: CGFloat = 1
    private var zoomObservation: NSKeyValueObservation?
    /// The last focus point set through `/set?focus=`, if any (camera queue).
    private var focusPoint: CGPoint?
    /// The latest on-screen zoom not yet written to the device (guarded by `lock`), so a burst
    /// of pinch or slider events becomes one write per trip through the camera queue.
    @ObservationIgnored private var pendingZoom: Double?
    /// On-screen zoom writes queued but not finished (main thread). While any are, KVO values
    /// are stale echoes of earlier writes, so they're ignored rather than snapping the UI back.
    @ObservationIgnored private var localZoomWrites = 0

    func start() {
        orientation.start()
        AVCaptureDevice.requestAccess(for: .video) { ok in
            guard ok else { self.setStatus("camera access denied"); return }
            self.queue.async { self.configure(); self.startServer() }
        }
    }

    private func configure() {
        let types: [AVCaptureDevice.DeviceType] = [.builtInTripleCamera, .builtInDualWideCamera, .builtInWideAngleCamera]
        guard let dev = types.lazy.compactMap({ AVCaptureDevice.default($0, for: .video, position: .back) }).first,
              let input = try? AVCaptureDeviceInput(device: dev) else { setStatus("no camera"); return }
        device = dev
        oneXFactor = dev.virtualDeviceSwitchOverVideoZoomFactors.first.map { CGFloat(truncating: $0) } ?? 1
        session.beginConfiguration()
        session.sessionPreset = session.canSetSessionPreset(.hd4K3840x2160) ? .hd4K3840x2160 : .high
        if session.canAddInput(input) { session.addInput(input) }
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        if session.canAddOutput(output) { session.addOutput(output) }
        session.commitConfiguration()
        session.startRunning()
        let oneX = oneXFactor
        let range = (Double(dev.minAvailableVideoZoomFactor / oneX), Double(dev.maxAvailableVideoZoomFactor / oneX))
        zoomObservation = dev.observe(\.videoZoomFactor, options: [.initial, .new]) { [weak self] d, _ in
            let z = Double(d.videoZoomFactor / oneX)
            DispatchQueue.main.async {
                guard let self else { return }
                (self.minZoom, self.maxZoom) = range
                if self.localZoomWrites == 0 { self.zoom = z }
            }
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pb = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let img = CIImage(cvPixelBuffer: pb)
        lock.lock(); latest = img; lock.unlock()
    }

    /// The factor the virtual device uses for the 1x (wide) lens.
    private var oneX: CGFloat { oneXFactor }

    /// Zoom from the on-screen controls (multiples of the 1x lens), clamped to the device's range
    /// exactly as `/set?zoom=` is.
    /// Call on the main thread.
    func setZoom(_ z: Double) {
        zoom = ZoomMath.clamp(z, minZoom, maxZoom)
        lock.lock()
        let alreadyQueued = pendingZoom != nil
        pendingZoom = z
        lock.unlock()
        guard !alreadyQueued else { return }
        localZoomWrites += 1
        queue.async {
            self.lock.lock()
            let target = self.pendingZoom
            self.pendingZoom = nil
            self.lock.unlock()
            var actual: Double?
            if let d = self.device, let target, (try? d.lockForConfiguration()) != nil {
                self.setZoomFactor(d, target)
                d.unlockForConfiguration()
                actual = Double(d.videoZoomFactor / self.oneX)
            }
            // Posted after this write's KVO echo, so the UI settles on what the device really did.
            DispatchQueue.main.async {
                self.localZoomWrites -= 1
                if self.localZoomWrites == 0, let actual { self.zoom = actual }
            }
        }
    }

    /// Call with the device locked for configuration.
    private func setZoomFactor(_ d: AVCaptureDevice, _ z: Double) {
        d.videoZoomFactor = ZoomMath.clamp(CGFloat(z) * oneX, d.minAvailableVideoZoomFactor, d.maxAvailableVideoZoomFactor)
    }

    private func apply(_ q: [String: String]) -> String {
        guard let d = device else { return "no device" }
        do {
            try d.lockForConfiguration()
            defer { d.unlockForConfiguration() }
            if let z = q["zoom"].flatMap(Double.init) {
                setZoomFactor(d, z)
            }
            if let f = q["focus"]?.split(separator: ",").compactMap({ Double($0) }), f.count == 2,
               d.isFocusPointOfInterestSupported {
                d.focusPointOfInterest = CGPoint(x: f[0], y: f[1])
                focusPoint = CGPoint(x: f[0], y: f[1])
                d.focusMode = .autoFocus
                if d.isExposurePointOfInterestSupported {
                    d.exposurePointOfInterest = CGPoint(x: f[0], y: f[1])
                    d.exposureMode = .autoExpose
                }
            }
            if q["lock"] == "1" {
                if d.isFocusModeSupported(.locked) { d.focusMode = .locked }
            } else if q["lock"] == "0", d.isFocusModeSupported(.continuousAutoFocus) {
                d.focusMode = .continuousAutoFocus
            }
            if let ev = q["ev"].flatMap(Float.init) {
                d.setExposureTargetBias(max(d.minExposureTargetBias, min(ev, d.maxExposureTargetBias)))
            }
        } catch { return "lock failed: \(error)" }
        return status()
    }

    private func status() -> String {
        guard let d = device else { return "{}" }
        let o = orientation.state
        return """
        {"zoom": \(String(format: "%.2f", d.videoZoomFactor / oneX)), "maxZoom": \(String(format: "%.1f", d.maxAvailableVideoZoomFactor / oneX)), \
        "lens": "\(d.activePrimaryConstituent?.localizedName ?? "?")", "focusMode": \(d.focusMode.rawValue), \
        "lensPosition": \(d.lensPosition), "ev": \(d.exposureTargetBias), \
        "orientation": "\(o.orientation.rawValue)", "rotation": \(o.rotation)}
        """
    }

    /// The current full-resolution frame (as `/snapshot.jpg` serves it) and the camera state,
    /// for pushing to the host. Blocks briefly on the camera queue, so call it off the main thread.
    func frameForSaving(note: String?, device name: String) -> (Data, FrameMeta)? {
        let taken = FrameMeta.timestamp(Date())
        // Take the frame and the state together on the camera queue, so they match; encode after.
        let captured: (CIImage, FrameMeta)? = queue.sync {
            lock.lock(); let img = latest; lock.unlock()
            guard let d = device, let img else { return nil }
            let o = orientation.state
            return (img, FrameMeta(
                zoom: FrameMeta.round(Double(d.videoZoomFactor / oneX), 2),
                lens: d.activePrimaryConstituent?.localizedName ?? "?",
                ev: FrameMeta.round(Double(d.exposureTargetBias), 2),
                focus: focusPoint.map { [FrameMeta.round($0.x, 3), FrameMeta.round($0.y, 3)] },
                lensPosition: FrameMeta.round(Double(d.lensPosition), 3),
                note: note, device: name, taken: taken,
                orientation: o.orientation.rawValue, rotation: o.rotation))
        }
        guard let (img, meta) = captured, let jpeg = jpeg(img) else { return nil }
        return (jpeg, meta)
    }

    /// The latest frame as a JPEG, raw or turned upright, and the orientation when it was taken.
    private func snapshot(upright: Bool) -> (Data, OrientationState)? {
        lock.lock(); let img = latest; lock.unlock()
        let o = orientation.state
        guard var img else { return nil }
        if upright { img = img.oriented(OrientationMath.imageOrientation(rotation: o.rotation)) }
        return jpeg(img).map { ($0, o) }
    }

    private func jpeg(_ img: CIImage) -> Data? {
        ciContext.jpegRepresentation(of: img, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
                                            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.92])
    }

    // MARK: HTTP

    private func startServer() {
        do {
            let l = try NWListener(using: .tcp, on: 8080)
            l.service = NWListener.Service(name: "ScreenEye", type: "_screeneye._tcp")
            l.newConnectionHandler = { [weak self] c in self?.handle(c) }
            l.stateUpdateHandler = { [weak self] s in self?.setStatus("ScreenEye :8080 — \(s)") }
            l.start(queue: .global())
            listener = l
        } catch { setStatus("listener failed: \(error)") }
    }

    private func handle(_ c: NWConnection) {
        c.start(queue: .global())
        c.receive(minimumIncompleteLength: 1, maximumLength: 8192) { [weak self] data, _, _, _ in
            guard let self, let data, let req = String(data: data, encoding: .utf8),
                  let line = req.split(separator: "\r\n").first else { c.cancel(); return }
            let target = line.split(separator: " ").dropFirst().first.map(String.init) ?? "/"
            let comps = URLComponents(string: target)
            let q = Dictionary((comps?.queryItems ?? []).map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { $1 })
            var body = Data(), type = "application/json", code = "200 OK", extra = ""
            switch comps?.path ?? "/" {
            case "/snapshot.jpg":
                let upright = ["1", "true", "yes"].contains(q["upright"]?.lowercased() ?? "")
                if let (j, o) = self.snapshot(upright: upright) {
                    body = j; type = "image/jpeg"
                    // The rotation still to apply: none if this frame is already upright.
                    extra = "X-ScreenEye-Orientation: \(o.orientation.rawValue)\r\n" +
                        "X-ScreenEye-Rotation: \(upright ? 0 : o.rotation)\r\n"
                } else { code = "503 Service Unavailable" }
            case "/set": body = Data(self.queue.sync { self.apply(q) }.utf8)
            case "/status": body = Data(self.queue.sync { self.status() }.utf8)
            default: code = "404 Not Found"
            }
            var head = "HTTP/1.1 \(code)\r\nContent-Type: \(type)\r\nContent-Length: \(body.count)\r\n\(extra)Connection: close\r\n\r\n"
            if body.isEmpty && code != "200 OK" { head = "HTTP/1.1 \(code)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" }
            c.send(content: Data(head.utf8) + body, completion: .contentProcessed { _ in c.cancel() })
        }
    }

    private func setStatus(_ s: String) {
        DispatchQueue.main.async { self.statusLine = s }
    }
}
