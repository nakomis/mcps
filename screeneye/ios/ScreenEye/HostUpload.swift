import Foundation

/// The `meta` field of `POST /api/frames`: the camera state when the frame was taken.
/// Absent values are sent as JSON `null` rather than left out.
struct FrameMeta: Encodable, Equatable, Sendable {
    var zoom: Double
    var lens: String
    var ev: Double
    var focus: [Double]?
    var lensPosition: Double
    var note: String?
    var device: String
    var taken: String
    /// Which way up the phone was (`DeviceOrientation`'s raw value).
    var orientation: String = DeviceOrientation.unknown.rawValue
    /// Clockwise degrees that turn the (raw, sensor-landscape) image upright: 0, 90, 180 or 270.
    var rotation: Int = 0

    private enum CodingKeys: String, CodingKey {
        case zoom, lens, ev, focus, lensPosition, note, device, taken, orientation, rotation
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(zoom, forKey: .zoom)
        try c.encode(lens, forKey: .lens)
        try c.encode(ev, forKey: .ev)
        if let focus { try c.encode(focus, forKey: .focus) } else { try c.encodeNil(forKey: .focus) }
        try c.encode(lensPosition, forKey: .lensPosition)
        if let note { try c.encode(note, forKey: .note) } else { try c.encodeNil(forKey: .note) }
        try c.encode(device, forKey: .device)
        try c.encode(taken, forKey: .taken)
        try c.encode(orientation, forKey: .orientation)
        try c.encode(rotation, forKey: .rotation)
    }

    func json() throws -> Data {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try e.encode(self)
    }

    /// A note with surrounding whitespace removed, or nil if nothing is left.
    static func cleanNote(_ note: String) -> String? {
        let t = note.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : t
    }

    /// Rounds to `places` decimal places, so Float noise (0.30000001…) doesn't reach the JSON.
    static func round(_ x: Double, _ places: Int) -> Double {
        let f = pow(10, Double(places))
        return (x * f).rounded() / f
    }

    /// ISO 8601 with a numeric offset, e.g. `2026-10-16T14:03:22+01:00` (never `Z`).
    static func timestamp(_ date: Date, timeZone: TimeZone = .current) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = timeZone
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssxxx"
        return f.string(from: date)
    }
}

/// A `multipart/form-data` body with the frame (`image`) and its metadata (`meta`).
struct Multipart {
    let boundary: String

    init(boundary: String = "ScreenEye-\(UUID().uuidString)") {
        self.boundary = boundary
    }

    var contentType: String { "multipart/form-data; boundary=\(boundary)" }

    func body(jpeg: Data, meta: Data) -> Data {
        var d = Data()
        func add(_ s: String) { d.append(Data(s.utf8)) }
        add("--\(boundary)\r\n")
        add("Content-Disposition: form-data; name=\"image\"; filename=\"frame.jpg\"\r\n")
        add("Content-Type: image/jpeg\r\n\r\n")
        d.append(jpeg)
        add("\r\n--\(boundary)\r\n")
        add("Content-Disposition: form-data; name=\"meta\"\r\n\r\n")
        d.append(meta)
        add("\r\n--\(boundary)--\r\n")
        return d
    }
}

/// Where frames are POSTed.
enum HostURL {
    static let defaultPath = "/api/frames"

    /// The frames URL for a host typed in Settings: `mace.local:8765`,
    /// `http://172.29.0.10:8765/` or the full `…/api/frames` all work.
    static func frames(base: String) -> URL? {
        var s = base.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "http://" + s }
        while s.hasSuffix("/") { s.removeLast() }
        guard var c = URLComponents(string: s),
              let scheme = c.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = c.host, !host.isEmpty else { return nil }
        if !c.path.hasSuffix(defaultPath) { c.path += defaultPath }
        return c.url
    }

    /// The frames URL for a host found over Bonjour, with the path from its TXT record.
    static func frames(host: String, port: Int, path: String?) -> URL? {
        var c = URLComponents()
        c.scheme = "http"
        c.host = host
        c.port = port
        let p = (path?.isEmpty == false) ? path! : defaultPath
        c.path = p.hasPrefix("/") ? p : "/" + p
        return c.url
    }
}

/// The host's `201` reply.
struct SavedFrame: Decodable, Equatable, Sendable {
    let name: String
    let path: String
}

enum SaveError: LocalizedError, Equatable {
    case noHost
    case noFrame
    case server(status: Int, message: String)
    case badResponse

    var errorDescription: String? {
        switch self {
        case .noHost: "No ScreenEye host found. Is it running on the Mac? You can also set its address in Settings."
        case .noFrame: "No camera frame yet."
        case .server(let status, let message): "Host said \(status): \(message)"
        case .badResponse: "The host's reply wasn't understood."
        }
    }

    /// The error for a non-201 reply: the host's `{"error": "…"}` if it sent one.
    static func from(status: Int, body: Data) -> SaveError {
        struct Reply: Decodable { let error: String }
        if let r = try? JSONDecoder().decode(Reply.self, from: body) {
            return .server(status: status, message: r.error)
        }
        let text = String(decoding: body.prefix(200), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return .server(status: status, message: text.isEmpty ? HTTPURLResponse.localizedString(forStatusCode: status) : text)
    }
}

enum Uploader {
    /// POSTs a frame and its metadata, reporting upload progress (0…1).
    static func post(to url: URL, jpeg: Data, meta: FrameMeta,
                     progress: @escaping @Sendable (Double) -> Void) async throws -> SavedFrame {
        let form = Multipart()
        var req = URLRequest(url: url, timeoutInterval: 30)
        req.httpMethod = "POST"
        req.setValue(form.contentType, forHTTPHeaderField: "Content-Type")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        let body = form.body(jpeg: jpeg, meta: try meta.json())
        let (data, response) = try await URLSession.shared.upload(for: req, from: body,
                                                                   delegate: ProgressDelegate(progress))
        guard let http = response as? HTTPURLResponse else { throw SaveError.badResponse }
        guard http.statusCode == 201 else { throw SaveError.from(status: http.statusCode, body: data) }
        guard let saved = try? JSONDecoder().decode(SavedFrame.self, from: data) else { throw SaveError.badResponse }
        return saved
    }

    private final class ProgressDelegate: NSObject, URLSessionTaskDelegate, Sendable {
        let report: @Sendable (Double) -> Void
        init(_ report: @escaping @Sendable (Double) -> Void) { self.report = report }

        func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                        totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
            guard totalBytesExpectedToSend > 0 else { return }
            report(Double(totalBytesSent) / Double(totalBytesExpectedToSend))
        }
    }
}
