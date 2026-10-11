import AVFoundation
import CoreImage
import Network
import Observation
import UIKit

/// Runs the back camera with full control and serves it over HTTP on port 8080:
///   GET /snapshot.jpg                    latest frame
///   GET /set?zoom=4&focus=0.5,0.5&lock=1&ev=-1   zoom (x of the 1x lens), focus point, lock, exposure bias
///   GET /status                          JSON
@Observable
final class Camera: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    let session = AVCaptureSession()
    var statusLine = "starting…"

    private var device: AVCaptureDevice?
    private let output = AVCaptureVideoDataOutput()
    private let queue = DispatchQueue(label: "camera")
    private let lock = NSLock()
    private var latest: CIImage?
    private let ciContext = CIContext()
    private var listener: NWListener?

    func start() {
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
        session.beginConfiguration()
        session.sessionPreset = session.canSetSessionPreset(.hd4K3840x2160) ? .hd4K3840x2160 : .high
        if session.canAddInput(input) { session.addInput(input) }
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        if session.canAddOutput(output) { session.addOutput(output) }
        session.commitConfiguration()
        session.startRunning()
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pb = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let img = CIImage(cvPixelBuffer: pb)
        lock.lock(); latest = img; lock.unlock()
    }

    /// The factor the virtual device uses for the 1x (wide) lens.
    private var oneX: CGFloat {
        guard let d = device else { return 1 }
        return d.virtualDeviceSwitchOverVideoZoomFactors.first.map { CGFloat(truncating: $0) } ?? 1
    }

    private func apply(_ q: [String: String]) -> String {
        guard let d = device else { return "no device" }
        do {
            try d.lockForConfiguration()
            defer { d.unlockForConfiguration() }
            if let z = q["zoom"].flatMap(Double.init) {
                d.videoZoomFactor = max(d.minAvailableVideoZoomFactor,
                                        min(CGFloat(z) * oneX, d.maxAvailableVideoZoomFactor))
            }
            if let f = q["focus"]?.split(separator: ",").compactMap({ Double($0) }), f.count == 2,
               d.isFocusPointOfInterestSupported {
                d.focusPointOfInterest = CGPoint(x: f[0], y: f[1])
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
        return """
        {"zoom": \(String(format: "%.2f", d.videoZoomFactor / oneX)), "maxZoom": \(String(format: "%.1f", d.maxAvailableVideoZoomFactor / oneX)), \
        "lens": "\(d.activePrimaryConstituent?.localizedName ?? "?")", "focusMode": \(d.focusMode.rawValue), \
        "lensPosition": \(d.lensPosition), "ev": \(d.exposureTargetBias)}
        """
    }

    private func snapshot() -> Data? {
        lock.lock(); let img = latest; lock.unlock()
        guard let img else { return nil }
        return ciContext.jpegRepresentation(of: img, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
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
            var body = Data(), type = "application/json", code = "200 OK"
            switch comps?.path ?? "/" {
            case "/snapshot.jpg":
                if let j = self.snapshot() { body = j; type = "image/jpeg" } else { code = "503 Service Unavailable" }
            case "/set": body = Data(self.queue.sync { self.apply(q) }.utf8)
            case "/status": body = Data(self.queue.sync { self.status() }.utf8)
            default: code = "404 Not Found"
            }
            var head = "HTTP/1.1 \(code)\r\nContent-Type: \(type)\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n"
            if body.isEmpty && code != "200 OK" { head = "HTTP/1.1 \(code)\r\nContent-Length: 0\r\nConnection: close\r\n\r\n" }
            c.send(content: Data(head.utf8) + body, completion: .contentProcessed { _ in c.cancel() })
        }
    }

    private func setStatus(_ s: String) {
        DispatchQueue.main.async { self.statusLine = s }
    }
}
