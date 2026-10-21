import CoreGraphics
import Foundation

/// The phone's `/status`, upright (`?upright=1`). Everything but the zoom is optional, so an
/// older build of the app (before EYE-3, EYE-4 or EYE-5) still decodes.
struct PhoneStatus: Decodable, Equatable, Sendable {
    var zoom: Double
    var maxZoom: Double
    /// From EYE-5; before that the phone didn't say, so 1.
    var minZoom: Double?
    /// The device's lens steps (multiples of the 1x lens), from EYE-5.
    var lensSteps: [Double]?
    var opticalZoom: Double?
    var lens: String?
    /// `AVCaptureDevice.FocusMode`: 0 locked, 1 auto, 2 continuous.
    var focusMode: Int?
    var lensPosition: Double?
    var ev: Double?
    var orientation: String?
    var rotation: Int?
    /// The viewport's centre, as fractions of the lens's frame.
    var pan: [Double]?
    /// What the picture covers of the lens's frame, `[x, y, w, h]`.
    var viewport: [Double]?
    var coordinates: String?

    var lower: Double { minZoom ?? 1 }

    /// The digital crop on top of the lens's own zoom: 1 is none, so there's nothing to pan.
    var digital: Double {
        guard let o = opticalZoom, o > 0 else { return 1 }
        return max(zoom / o, 1)
    }

    var canPan: Bool { digital > 1.001 }
    var focusLocked: Bool { focusMode == 0 }

    var panPoint: CGPoint {
        guard let p = pan, p.count == 2 else { return CGPoint(x: 0.5, y: 0.5) }
        return CGPoint(x: p[0], y: p[1])
    }

    /// The viewport as a rectangle (fractions of the lens's frame), the whole frame if unknown.
    var viewportRect: CGRect {
        guard let v = viewport, v.count == 4 else { return CGRect(x: 0, y: 0, width: 1, height: 1) }
        return CGRect(x: v[0], y: v[1], width: v[2], height: v[3])
    }
}

/// The phone's HTTP API (see the README), always in upright coordinates.
struct PhoneAPI: Sendable {
    let base: URL

    /// A base URL from what was typed: `172.29.0.40:8080`, `http://iphone.local:8080/`.
    /// Port 8080 if none is given.
    static func base(from typed: String) -> URL? {
        var s = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "http://" + s }
        while s.hasSuffix("/") { s.removeLast() }
        guard var c = URLComponents(string: s), c.scheme?.lowercased() == "http",
              let host = c.host, !host.isEmpty else { return nil }
        if c.port == nil { c.port = 8080 }
        c.path = ""
        c.query = nil
        return c.url
    }

    static func base(host: String, port: Int) -> URL? {
        var c = URLComponents()
        c.scheme = "http"
        c.host = host
        c.port = port
        return c.url
    }

    /// The picture, upright. `full` takes the lens's whole frame; `maxDim` asks the phone to
    /// scale it down (ignored by builds before EYE-5, which send it full size).
    func snapshotURL(full: Bool = false, maxDim: Int? = nil) -> URL {
        var items = [URLQueryItem(name: "upright", value: "1")]
        if full { items.append(URLQueryItem(name: "full", value: "1")) }
        if let maxDim { items.append(URLQueryItem(name: "max", value: "\(maxDim)")) }
        return url("/snapshot.jpg", items)
    }

    var statusURL: URL { url("/status", [URLQueryItem(name: "upright", value: "1")]) }

    /// `/set` with the given changes (keys `zoom`, `pan`, `focus`, `lock`, `ev`), upright.
    func setURL(_ changes: [String: String]) -> URL {
        url("/set", changes.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
            + [URLQueryItem(name: "upright", value: "1")])
    }

    private func url(_ path: String, _ items: [URLQueryItem]) -> URL {
        var c = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        c.path = path
        c.queryItems = items
        return c.url!
    }

    // MARK: Values for /set

    static func number(_ x: Double) -> String {
        String(format: "%.4f", x).replacingOccurrences(of: #"\.?0+$"#, with: "", options: .regularExpression)
    }

    static func point(_ p: CGPoint) -> String { "\(number(p.x)),\(number(p.y))" }
}

/// Choosing among the phones found over Bonjour, in "first found" mode.
enum PhoneChoice {
    /// How long the phone showing may go without a frame before another is tried.
    static let patience: TimeInterval = 6

    /// The order to try phones in: the one showing now first (so a change to the list doesn't
    /// drop a phone that's working), then the rest as listed.
    static func order(_ names: [String], current: String) -> [String] {
        guard let i = names.firstIndex(of: current) else { return names }
        return Array(names[i...] + names[..<i])
    }

    /// Whether to give up on the phone showing: it's gone quiet, and there's another to try.
    static func shouldMoveOn(silentFor: TimeInterval, phones: Int) -> Bool {
        phones > 1 && silentFor >= patience
    }
}

/// Picture geometry on the Mac: the picture is the upright viewport, drawn fitted in the view.
enum PictureMath {
    /// Where a picture of `size` is drawn, fitted and centred in `bounds`.
    static func fitted(_ size: CGSize, in bounds: CGSize) -> CGRect {
        guard size.width > 0, size.height > 0, bounds.width > 0, bounds.height > 0 else {
            return CGRect(origin: .zero, size: bounds)
        }
        let s = min(bounds.width / size.width, bounds.height / size.height)
        let w = size.width * s, h = size.height * s
        return CGRect(x: (bounds.width - w) / 2, y: (bounds.height - h) / 2, width: w, height: h)
    }

    /// A point in the view as fractions of the picture drawn in `rect`, or nil if it's outside.
    static func fraction(of p: CGPoint, in rect: CGRect) -> CGPoint? {
        guard rect.width > 0, rect.height > 0, rect.contains(p) else { return nil }
        return CGPoint(x: (p.x - rect.minX) / rect.width, y: (p.y - rect.minY) / rect.height)
    }

    /// The pan (the viewport's centre, fractions of the lens's frame) after dragging the
    /// picture, drawn `drawn` points across, by `translation`: the picture follows the pointer,
    /// so the centre moves the other way, by the drag's share of the viewport.
    static func dragged(pan: CGPoint, viewport: CGRect, translation: CGSize, drawn: CGSize) -> CGPoint {
        guard drawn.width > 0, drawn.height > 0 else { return pan }
        func clamp(_ v: Double, _ half: Double) -> Double { min(max(v, half), 1 - half) }
        return CGPoint(
            x: clamp(pan.x - translation.width / drawn.width * viewport.width, viewport.width / 2),
            y: clamp(pan.y - translation.height / drawn.height * viewport.height, viewport.height / 2))
    }
}
