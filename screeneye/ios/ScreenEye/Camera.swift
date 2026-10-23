import AVFoundation
import CoreImage
import Network
import Observation
import UIKit

/// Runs the back camera with full control and serves it over HTTP on port 8080:
///   GET /snapshot.jpg        the picture: the visible region (the viewport) of the latest frame,
///                            raw (sensor landscape). Headers X-ScreenEye-Orientation,
///                            X-ScreenEye-Rotation and X-ScreenEye-Viewport
///     ?upright=1             turned upright
///     ?full=1                the lens's whole frame rather than the viewport
///     ?max=1280              scaled down so its longer side is at most this many pixels
///   GET /set?zoom=4&pan=0.3,0.6&focus=0.5,0.5&lock=1&ev=-1
///                            zoom (x of the 1x lens), viewport centre (fractions of the lens's
///                            frame), focus point (fractions of the picture), lock, exposure bias.
///     &upright=1             pan and focus are in the upright frame rather than the raw one
///   GET /status[?upright=1]  JSON, with orientation, rotation, opticalZoom, pan, viewport,
///                            minZoom and lensSteps
///
/// Zoom is the lens's own zoom (the nearest lens step at or below) and then a digital crop of
/// its frame, so the crop can sit anywhere in the frame: that's panning.
@Observable
final class Camera: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    let session = AVCaptureSession()
    var statusLine = "starting…"

    // Mirrors for the on-screen controls (main thread). The truth is `view`, under `lock`.
    /// The zoom, in multiples of the 1x lens.
    var zoom: Double = 1
    var minZoom: Double = 1
    var maxZoom: Double = 1
    /// What's visible: zoom, lens step and centre.
    var viewport = Viewport()
    /// The device's lens steps, for the zoom buttons.
    var lensSteps: [Double] = [1]
    /// The raw frame's width / height.
    var frameAspect: Double = 16.0 / 9.0
    /// A small picture of the lens's whole frame, raw, for the overview while zoomed in digitally.
    var overview: CGImage?

    private var device: AVCaptureDevice?
    private let orientation = OrientationTracker()
    private let output = AVCaptureVideoDataOutput()
    private let queue = DispatchQueue(label: "camera")
    /// Guards `latest`, `latestOptical`, `view`, `lenses`, `zoomRange` and the lens fields.
    private let lock = NSLock()
    private var latest: CIImage?
    /// The lens step `latest` was taken at.
    @ObservationIgnored private var latestOptical = 1.0
    /// The lens step last set on the device, the one before, and when: frames keep coming from
    /// the old lens for a moment after a switch.
    @ObservationIgnored private var appliedOptical = 1.0
    @ObservationIgnored private var previousOptical = 1.0
    @ObservationIgnored private var lensMovedAt = Date.distantPast
    /// How long after a lens switch frames are taken to still be from the old lens.
    static let lensSettle: TimeInterval = 0.3
    @ObservationIgnored private var view = Viewport()
    /// Lens steps (multiples of the 1x lens) the device can zoom to at full quality.
    @ObservationIgnored private var lenses: [Double] = [1]
    @ObservationIgnored private var zoomRange = (1.0, 1.0)
    private let ciContext = CIContext()
    private var listener: NWListener?
    private var oneXFactor: CGFloat = 1
    /// The last focus point set, as fractions of the lens's frame, if any (camera queue).
    private var focusPoint: CGPoint?
    // Camera queue only.
    @ObservationIgnored private var lastOverview = Date.distantPast
    @ObservationIgnored private var lastAspect = 0.0

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

        let oneX = Double(oneXFactor)
        let range = (Double(dev.minAvailableVideoZoomFactor) / oneX, Double(dev.maxAvailableVideoZoomFactor) / oneX)
        // The lenses, plus the zooms a lens can crop its sensor to at full output resolution
        // (the 48 MP main camera's 2x, for one).
        let factors = [Double(dev.minAvailableVideoZoomFactor)]
            + dev.virtualDeviceSwitchOverVideoZoomFactors.map { $0.doubleValue }
            + dev.activeFormat.secondaryNativeResolutionZoomFactors.map { Double($0) }
        let steps = Set(factors.map { ($0 / oneX * 1000).rounded() / 1000 })
            .filter { $0 >= range.0 - 0.001 && $0 <= range.1 + 0.001 }.sorted()
        lock.lock()
        lenses = steps.isEmpty ? [range.0] : steps
        zoomRange = range
        lock.unlock()
        let start = setView(zoom: Double(dev.videoZoomFactor) / oneX, center: nil)
        lock.lock()
        // Whatever the device started at, frames are taken as at this step; moveLens puts it there.
        (appliedOptical, previousOptical) = (start.optical, start.optical)
        lock.unlock()
        moveLens(attempt: 1)
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pb = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let img = CIImage(cvPixelBuffer: pb)
        lock.lock()
        latest = img
        latestOptical = Date().timeIntervalSince(lensMovedAt) < Self.lensSettle ? previousOptical : appliedOptical
        let digital = view.digital
        lock.unlock()
        let e = img.extent
        let aspect = e.height > 0 ? Double(e.width / e.height) : 16.0 / 9.0
        if aspect != lastAspect {
            lastAspect = aspect
            DispatchQueue.main.async { self.frameAspect = aspect }
        }
        // The overview, twice a second while there's a crop to show it against.
        let now = Date()
        if digital > 1.001, now.timeIntervalSince(lastOverview) > 0.5 {
            lastOverview = now
            let s = 240 / max(e.width, e.height)
            let small = img.transformed(by: CGAffineTransform(scaleX: s, y: s))
            let cg = ciContext.createCGImage(small, from: small.extent)
            DispatchQueue.main.async { self.overview = cg }
        }
    }

    // MARK: Zoom and pan

    /// Changes the zoom and/or centre (raw fractions of the lens's frame), clamped to what the
    /// device can do, and moves the lens if its step changed. Safe from any thread.
    @discardableResult
    func setView(zoom: Double?, center: CGPoint?) -> Viewport {
        lock.lock()
        let before = view
        view = ViewportMath.make(zoom: zoom ?? view.zoom, center: center ?? view.center,
                                 lenses: lenses, min: zoomRange.0, max: zoomRange.1)
        let after = view
        lock.unlock()
        if after.optical != before.optical {
            queue.async { self.moveLens() }
        }
        publish()
        return after
    }

    /// Recentres the viewport (double tap).
    func recentre() { setView(zoom: nil, center: CGPoint(x: 0.5, y: 0.5)) }

    /// Zoom from the on-screen controls (multiples of the 1x lens). Call on the main thread.
    func setZoom(_ z: Double) { setView(zoom: z, center: nil) }

    /// Puts the lens at the current step (camera queue). Reads the step when it runs, so a burst
    /// of changes ends on the last; does nothing if it's already there. If the device can't be
    /// locked, tries again shortly rather than leave the lens and the viewport disagreeing.
    private func moveLens(attempt: Int = 0) {
        lock.lock(); let optical = view.optical; let applied = appliedOptical; lock.unlock()
        guard let d = device else { return }
        let factor = ZoomMath.clamp(CGFloat(optical) * oneXFactor, d.minAvailableVideoZoomFactor, d.maxAvailableVideoZoomFactor)
        if optical == applied && d.videoZoomFactor == factor { return }
        guard (try? d.lockForConfiguration()) != nil else {
            if attempt < 20 { queue.asyncAfter(deadline: .now() + 0.1) { self.moveLens(attempt: attempt + 1) } }
            return
        }
        d.videoZoomFactor = factor
        d.unlockForConfiguration()
        lock.lock()
        if optical != appliedOptical {
            previousOptical = appliedOptical
            appliedOptical = optical
            lensMovedAt = Date()
        }
        lock.unlock()
    }

    /// Copies the view state to the main-thread mirrors.
    private func publish() {
        if Thread.isMainThread { mirror() } else { DispatchQueue.main.async { self.mirror() } }
    }

    /// Main thread: reads the latest state when it runs, so mirrors arriving out of order still
    /// end on the newest.
    private func mirror() {
        lock.lock()
        let (v, range, steps) = (view, zoomRange, lenses)
        lock.unlock()
        (minZoom, maxZoom) = range
        if lensSteps != steps { lensSteps = steps }
        zoom = v.zoom
        if viewport != v { viewport = v }
    }

    private var currentView: Viewport {
        lock.lock(); defer { lock.unlock() }
        return view
    }

    // MARK: /set and /status

    /// Applies `/set` (camera queue).
    private func apply(_ q: [String: String]) -> String {
        guard let d = device else { return "{}" }
        let rotation = Self.flag(q["upright"]) ? orientation.state.rotation : 0
        let pan = Self.point(q["pan"]).map { FrameMath.raw($0, rotation: rotation) }
        let zoom = q["zoom"].flatMap(Double.init)
        let v = (zoom != nil || pan != nil) ? setView(zoom: zoom, center: pan) : currentView
        moveLens()  // now, so focus is set on the lens the picture comes from
        do {
            try d.lockForConfiguration()
            defer { d.unlockForConfiguration() }
            if let f = Self.point(q["focus"]), d.isFocusPointOfInterestSupported {
                // A point in the picture, raw, then in the lens's frame (the device's coordinates).
                let p = v.framePoint(FrameMath.raw(f, rotation: rotation))
                d.focusPointOfInterest = p
                focusPoint = p
                d.focusMode = .autoFocus
                if d.isExposurePointOfInterestSupported {
                    d.exposurePointOfInterest = p
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
        } catch { return "{\"error\": \"lock failed: \(error)\"}" }
        return status(upright: Self.flag(q["upright"]))
    }

    /// `/status` as JSON. With `upright`, pan and viewport are in the upright frame.
    private func status(upright: Bool) -> String {
        guard let d = device else { return "{}" }
        let o = orientation.state
        let v = currentView
        lock.lock(); let (range, steps) = (zoomRange, lenses); lock.unlock()
        let r = upright ? o.rotation : 0
        let c = FrameMath.turned(v.center, rotation: r)
        let rect = FrameMath.turned(v.rect, rotation: r)
        let obj: [String: Any] = [
            "zoom": FrameMeta.round(v.zoom, 2),
            "maxZoom": FrameMeta.round(Double(d.maxAvailableVideoZoomFactor / oneXFactor), 1),
            "minZoom": FrameMeta.round(range.0, 2),
            "lensSteps": steps.map { FrameMeta.round($0, 2) },
            "opticalZoom": FrameMeta.round(v.optical, 2),
            "lens": d.activePrimaryConstituent?.localizedName ?? "?",
            "focusMode": d.focusMode.rawValue,
            "lensPosition": FrameMeta.round(Double(d.lensPosition), 3),
            "ev": FrameMeta.round(Double(d.exposureTargetBias), 2),
            "orientation": o.orientation.rawValue,
            "rotation": o.rotation,
            "pan": [FrameMeta.round(c.x, 4), FrameMeta.round(c.y, 4)],
            "viewport": Self.rounded(rect),
            "coordinates": upright ? "upright" : "raw",
        ]
        let data = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data("{}".utf8)
        return String(decoding: data, as: UTF8.self)
    }

    static func flag(_ s: String?) -> Bool { ["1", "true", "yes"].contains(s?.lowercased() ?? "") }

    /// "x,y" as a point, if it is two finite numbers.
    static func point(_ s: String?) -> CGPoint? {
        guard let f = s?.split(separator: ",").compactMap({ Double($0.trimmingCharacters(in: .whitespaces)) }),
              f.count == 2, f.allSatisfy(\.isFinite) else { return nil }
        return CGPoint(x: f[0], y: f[1])
    }

    static func rounded(_ r: CGRect) -> [Double] {
        [r.minX, r.minY, r.width, r.height].map { FrameMeta.round($0, 4) }
    }

    // MARK: Frames

    /// The visible region of a frame, at the frame's full resolution.
    static func crop(_ img: CIImage, to r: CGRect) -> CIImage {
        let e = img.extent
        // Fractions from the top-left; Core Image's origin is the bottom-left.
        let px = CGRect(x: e.minX + r.minX * e.width, y: e.minY + (1 - r.maxY) * e.height,
                        width: r.width * e.width, height: r.height * e.height).integral.intersection(e)
        guard !px.isEmpty else { return img }
        return img.cropped(to: px).transformed(by: CGAffineTransform(translationX: -px.minX, y: -px.minY))
    }

    /// The picture (the visible region at full resolution, as `/snapshot.jpg` serves it) and the
    /// camera state, for pushing to the host. Blocks briefly on the camera queue, so call it off
    /// the main thread.
    func frameForSaving(note: String?, device name: String) -> (Data, FrameMeta)? {
        let taken = FrameMeta.timestamp(Date())
        // Take the frame and the state together on the camera queue, so they match; encode after.
        let captured: (CIImage, FrameMeta)? = queue.sync {
            lock.lock(); let img = latest; let v = view; let frameOptical = latestOptical; lock.unlock()
            guard let d = device, let img else { return nil }
            let o = orientation.state
            let rect = v.rect(forFrameAt: frameOptical)
            return (Self.crop(img, to: rect), FrameMeta(
                zoom: FrameMeta.round(v.zoom, 2),
                lens: d.activePrimaryConstituent?.localizedName ?? "?",
                ev: FrameMeta.round(Double(d.exposureTargetBias), 2),
                focus: focusPoint.map { [FrameMeta.round($0.x, 3), FrameMeta.round($0.y, 3)] },
                lensPosition: FrameMeta.round(Double(d.lensPosition), 3),
                note: note, device: name, taken: taken,
                orientation: o.orientation.rawValue, rotation: o.rotation,
                opticalZoom: FrameMeta.round(frameOptical, 2),
                frame: [Int(img.extent.width), Int(img.extent.height)],
                crop: Self.rounded(rect)))
        }
        guard let (img, meta) = captured, let jpeg = jpeg(img) else { return nil }
        return (jpeg, meta)
    }

    /// The picture turned upright, at full resolution, for parcel-finder (`/snapshot.jpg?upright=1`).
    /// Call it off the main thread.
    func uprightFrame(maxDim: Double? = nil) -> Data? { snapshot(upright: true, full: false, maxDim: maxDim)?.0 }

    /// The latest frame as a JPEG (the viewport, or the whole frame), raw or turned upright,
    /// with the orientation and viewport it was taken with. `maxDim` scales it down so its longer
    /// side is at most that many pixels.
    private func snapshot(upright: Bool, full: Bool, maxDim: Double? = nil) -> (Data, OrientationState, CGRect)? {
        lock.lock(); let img = latest; let v = view; let frameOptical = latestOptical; lock.unlock()
        let o = orientation.state
        guard var img else { return nil }
        let rect = full ? CGRect(x: 0, y: 0, width: 1, height: 1) : v.rect(forFrameAt: frameOptical)
        if !full { img = Self.crop(img, to: rect) }
        if upright { img = img.oriented(OrientationMath.imageOrientation(rotation: o.rotation)) }
        let longest = max(img.extent.width, img.extent.height)
        if let maxDim, maxDim >= 16, longest > maxDim {
            let s = maxDim / longest
            img = img.transformed(by: CGAffineTransform(scaleX: s, y: s))
        }
        return jpeg(img).map { ($0, o, rect) }
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
                let upright = Self.flag(q["upright"])
                if let (j, o, rect) = self.snapshot(upright: upright, full: Self.flag(q["full"]),
                                                      maxDim: q["max"].flatMap(Double.init)) {
                    body = j; type = "image/jpeg"
                    // The rotation still to apply: none if this frame is already upright.
                    // The viewport is always raw fractions of the lens's frame.
                    extra = "X-ScreenEye-Orientation: \(o.orientation.rawValue)\r\n" +
                        "X-ScreenEye-Rotation: \(upright ? 0 : o.rotation)\r\n" +
                        "X-ScreenEye-Viewport: \(Self.rounded(rect).map { "\($0)" }.joined(separator: ","))\r\n"
                } else { code = "503 Service Unavailable" }
            case "/set": body = Data(self.queue.sync { self.apply(q) }.utf8)
            case "/status": body = Data(self.queue.sync { self.status(upright: Self.flag(q["upright"])) }.utf8)
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
