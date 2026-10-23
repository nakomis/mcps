import Foundation
import ImageIO
import Observation
import Security
import Vision

// "Open in Claude" on the phone: parcel-finder on the Mac (github.com/nakomis/parcel-finder) finds
// the Claude Code session a newly arrived part was bought in and hands it the photo. The phone
// reads the text on it with Vision, uploads both, then follows the run and answers its pick-list.

/// What Vision reads from the picture: confident lines only (its junk lines score about 0.3).
enum ParcelOCR {
    static let minConfidence: Float = 0.5
    /// Vision reads small silkscreen better from a downscaled copy than from the full 4K frame.
    static let maxDim = 1600

    static func text(lines: [(String, Float)]) -> String? {
        let kept = lines.filter { $0.1 >= minConfidence }
            .map { $0.0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return kept.isEmpty ? nil : kept.joined(separator: "\n")
    }

    static func request() -> VNRecognizeTextRequest {
        let r = VNRecognizeTextRequest()
        r.recognitionLevel = .accurate
        r.usesLanguageCorrection = false  // part numbers aren't words
        return r
    }

    /// The text on a JPEG, read from a copy at most `maxDim` pixels on its longer side.
    static func read(jpeg: Data) -> String? {
        guard let src = CGImageSourceCreateWithData(jpeg as CFData, nil),
              let img = CGImageSourceCreateThumbnailAtIndex(src, 0, [
                  kCGImageSourceCreateThumbnailFromImageAlways: true,
                  kCGImageSourceThumbnailMaxPixelSize: maxDim,
                  kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary) else { return nil }
        let r = request()
        try? VNImageRequestHandler(cgImage: img).perform([r])
        return text(lines: (r.results ?? []).compactMap { o in o.topCandidates(1).first.map { ($0.string, $0.confidence) } })
    }

    /// The first recognition loads Vision's model (about 24 s cold on a Mac): do it at launch.
    static func warmUp() {
        let ctx = CGContext(data: nil, width: 64, height: 64, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)
        guard let img = ctx?.makeImage() else { return }
        try? VNImageRequestHandler(cgImage: img).perform([request()])
    }
}

/// A QR code in a picture (the pairing code `parcel-finder pair` shows on the Mac).
enum ParcelQR {
    static func payload(jpeg: Data) -> String? {
        guard let src = CGImageSourceCreateWithData(jpeg as CFData, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return nil }
        let r = VNDetectBarcodesRequest()
        r.symbologies = [.qr]
        try? VNImageRequestHandler(cgImage: img).perform([r])
        return r.results?.compactMap(\.payloadStringValue).first
    }
}

/// The token from `parcel-finder pair`, kept in the keychain.
enum ParcelPairing {
    private static let service = "com.nakomis.screeneye.parcel-finder"

    /// The token in a scanned QR code (`{"parcel_finder_token": "…"}`) or pasted on its own.
    static func token(from s: String) -> String? {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.hasPrefix("{") {
            struct Code: Decodable { let parcel_finder_token: String }
            return (try? JSONDecoder().decode(Code.self, from: Data(t.utf8)))?.parcel_finder_token
        }
        return t.count >= 20 && t.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }) ? t : nil
    }

    static var token: String? {
        var out: CFTypeRef?
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecReturnData as String: true]
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(decoding: d, as: UTF8.self)
    }

    static func save(_ token: String) {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service]
        SecItemDelete(q as CFDictionary)
        var add = q
        add[kSecValueData as String] = Data(token.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }

    static func forget() {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service] as CFDictionary)
    }
}

/// A run's status, as `GET /runs/<id>` reports it.
struct ParcelRun: Decodable, Equatable, Sendable, Identifiable {
    enum State: String, Decodable, Sendable { case queued, running, waiting, done }

    struct Choice: Decodable, Equatable, Sendable, Identifiable {
        let sessionID: String
        let title: String
        let when: String
        let hits: Int
        var id: String { sessionID }
        enum CodingKeys: String, CodingKey { case sessionID = "session_id", title, when, hits }
    }

    let runID: String
    let state: State
    let step: String
    let item: String?
    let session: String?
    let outcome: String?
    let detail: String
    let choices: [Choice]

    enum CodingKeys: String, CodingKey { case runID = "run_id", state, step, item, session, outcome, detail, choices }

    var id: String { runID }
    var isFinished: Bool { state == .done }
    var succeeded: Bool { outcome == "delivered" || outcome == "resumed" }

    var summary: String {
        switch outcome {
        case "delivered": "Sent to \(session ?? "the conversation")"
        case "resumed": "Opened \(session ?? "the conversation")"
        case "cancelled": "Cancelled"
        case "not-found": "Couldn't find the conversation for the \(item ?? "item")"
        default: "parcel-finder failed: \(detail.isEmpty ? (outcome ?? "unknown") : detail)"
        }
    }
}

enum ParcelError: LocalizedError, Equatable {
    case notPaired, noMac, noFrame, server(Int, String), badResponse

    var errorDescription: String? {
        switch self {
        case .notPaired: "Not paired with parcel-finder: run `parcel-finder pair` on the Mac, then scan its code in Settings."
        case .noMac: "parcel-finder not found on this network. Is the Mac awake, with a phone paired?"
        case .noFrame: "No camera frame yet."
        case .server(let status, let message): "parcel-finder said \(status): \(message)"
        case .badResponse: "parcel-finder's reply wasn't understood."
        }
    }
}

/// parcel-finder's phone API (see its `phone.py`).
struct ParcelClient: Sendable {
    let base: URL
    let token: String

    static func runBody(jpeg: Data, ocr: String?) throws -> Data {
        struct Body: Encodable {
            let photo: String
            let ocr: String?
            func encode(to e: Encoder) throws {
                var c = e.container(keyedBy: CodingKeys.self)
                try c.encode(photo, forKey: .photo)
                if let ocr { try c.encode(ocr, forKey: .ocr) } else { try c.encodeNil(forKey: .ocr) }
            }
            enum CodingKeys: String, CodingKey { case photo, ocr }
        }
        return try JSONEncoder().encode(Body(photo: jpeg.base64EncodedString(), ocr: ocr))
    }

    func start(jpeg: Data, ocr: String?) async throws -> ParcelRun {
        try await send("/runs", body: try Self.runBody(jpeg: jpeg, ocr: ocr), timeout: 60)
    }

    func status(_ id: String) async throws -> ParcelRun { try await send("/runs/\(id)", body: nil, timeout: 10) }

    func choose(_ id: String, sessionID: String?) async throws -> ParcelRun {
        let body = try JSONSerialization.data(withJSONObject: ["session_id": sessionID as Any? ?? NSNull()])
        return try await send("/runs/\(id)/choice", body: body, timeout: 10)
    }

    private func send(_ path: String, body: Data?, timeout: TimeInterval) async throws -> ParcelRun {
        var req = URLRequest(url: base.appendingPathComponent(path), timeoutInterval: timeout)
        req.httpMethod = body == nil ? "GET" : "POST"
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = body
        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw ParcelError.badResponse }
        guard (200..<300).contains(http.statusCode) else {
            struct Reply: Decodable { let error: String }
            throw ParcelError.server(http.statusCode, (try? JSONDecoder().decode(Reply.self, from: data))?.error ?? "")
        }
        guard let run = try? JSONDecoder().decode(ParcelRun.self, from: data) else { throw ParcelError.badResponse }
        return run
    }
}

/// One "Open in Claude" at a time: take the frame, read it, upload, follow the run.
@MainActor @Observable
final class ParcelRunner {
    enum State: Equatable {
        case idle
        case preparing(String)
        case running(ParcelRun)
        case finished(ParcelRun)
        case failed(String)
    }

    private(set) var state: State = .idle
    private var client: ParcelClient?

    var isBusy: Bool {
        switch state {
        case .preparing, .running: true
        default: false
        }
    }

    /// The run while it's waiting for the pick-list's answer.
    var waiting: ParcelRun? {
        if case .running(let r) = state, r.state == .waiting, !r.choices.isEmpty { return r }
        return nil
    }

    func open(camera: Camera, finder: HostFinder) {
        guard !isBusy else { return }
        guard let token = ParcelPairing.token else { state = .failed(ParcelError.notPaired.localizedDescription); return }
        state = .preparing("Taking the picture…")
        Task {
            do {
                guard let jpeg = await Task.detached(operation: { camera.uprightFrame() }).value else { throw ParcelError.noFrame }
                state = .preparing("Reading the label…")
                let ocr = await Task.detached(operation: { ParcelOCR.read(jpeg: jpeg) }).value
                state = .preparing("Finding parcel-finder…")
                guard let base = try? await finder.baseURL() else { throw ParcelError.noMac }
                let client = ParcelClient(base: base, token: token)
                self.client = client
                state = .preparing("Sending…")
                var run = try await client.start(jpeg: jpeg, ocr: ocr)
                while !run.isFinished {
                    state = .running(run)
                    try await Task.sleep(for: .milliseconds(500))
                    run = try await client.status(run.runID)
                }
                state = .finished(run)
            } catch {
                state = .failed(error.localizedDescription)
            }
        }
    }

    /// The pick-list's answer: a session, or nil to cancel the run.
    func choose(_ sessionID: String?) {
        guard let run = waiting, let client else { return }
        Task { _ = try? await client.choose(run.runID, sessionID: sessionID) }
    }

    func dismiss() { if !isBusy { state = .idle } }
}
