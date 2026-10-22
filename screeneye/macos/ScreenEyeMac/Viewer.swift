import AppKit
import Foundation
import Observation

enum ViewerError: LocalizedError {
    case notFound
    case noPhone
    case http(Int)
    case notAnImage

    var errorDescription: String? {
        switch self {
        case .notFound: "Couldn't reach the phone found over Bonjour."
        case .noPhone: "No phone yet. Is ScreenEye open on it? You can also type its address in Settings."
        case .http(let code): "The phone said \(code) \(HTTPURLResponse.localizedString(forStatusCode: code))."
        case .notAnImage: "The phone's picture couldn't be read."
        }
    }
}

/// Settings keys (UserDefaults).
enum Keys {
    static let manualURL = "phoneURL"
    static let folder = "folder"
    static let copyAfterTaking = "copyAfterTaking"
    static let previewSize = "previewSize"
    static let defaultPreviewSize = 1600
}

/// The live view of one phone: frames polled from `/snapshot.jpg`, its `/status`, and the
/// camera changes sent to `/set`.
@MainActor @Observable
final class Viewer {
    enum Taken: Equatable {
        case idle
        case taking
        case saved(URL)
        case failed(String)
    }

    /// The phone being shown, and its name.
    private(set) var api: PhoneAPI?
    private(set) var phoneName = ""
    private(set) var frame: NSImage?
    /// The lens's whole frame, small, while zoomed in digitally.
    private(set) var overview: NSImage?
    private(set) var status: PhoneStatus?
    private(set) var problem: String?
    private(set) var framesPerSecond = 0.0
    private(set) var taken: Taken = .idle
    /// A drag waiting for the phone: the picture is drawn offset by this until a frame arrives
    /// that was taken after the pan was set.
    var dragOffset = CGSize.zero
    /// Pausing stops fetching frames, not the phone's camera.
    var paused = false
    /// Added to the next picture's name and sidecar.
    var note = ""

    private let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 10
        c.requestCachePolicy = .reloadIgnoringLocalCacheData
        c.httpMaximumConnectionsPerHost = 4
        return URLSession(configuration: c)
    }()
    private var loop: Task<Void, Never>?
    /// Changes waiting to go to `/set`, merged so a burst (the zoom slider) sends only the last.
    private var pending: [String: String] = [:]
    private var sending = false
    /// Bumped by each change, so frames fetched before it don't clear the drag offset.
    private var generation = 0
    private var frameTimes: [Date] = []
    /// When the phone was connected to, and when it last sent a frame.
    private var connectedAt = Date()
    private var lastFrameAt: Date?

    /// How long the phone has gone without sending a frame (0 while paused or with no phone).
    var silentFor: TimeInterval {
        guard api != nil, !paused else { return 0 }
        return Date().timeIntervalSince(lastFrameAt ?? connectedAt)
    }
    private var lastOverview = Date.distantPast

    // MARK: Connecting

    /// Shows `api`'s phone; with none, shows `problem` (or why there's no phone).
    func connect(to api: PhoneAPI?, name: String, problem: String? = nil) {
        if let api, api.base == self.api?.base { return }
        loop?.cancel()
        self.api = api
        phoneName = name
        frame = nil
        overview = nil
        status = nil
        framesPerSecond = 0
        connectedAt = Date()
        lastFrameAt = nil
        self.problem = api == nil ? problem ?? ViewerError.noPhone.localizedDescription : nil
        guard let api else { return }
        loop = Task { [weak self] in await self?.run(api) }
    }

    private func run(_ api: PhoneAPI) async {
        await refreshStatus(api)
        var lastStatus = Date()
        while !Task.isCancelled {
            if paused {
                lastFrameAt = Date()  // a pause isn't the phone going quiet
                try? await Task.sleep(for: .milliseconds(250))
                continue
            }
            let started = generation
            do {
                let (data, _) = try await get(api.snapshotURL(maxDim: previewSize))
                guard !Task.isCancelled else { return }
                guard let img = NSImage(data: data) else { throw ViewerError.notAnImage }
                frame = img
                lastFrameAt = Date()
                problem = nil
                if started == generation && !sending && pending.isEmpty { dragOffset = .zero }
                countFrame()
            } catch {
                guard !Task.isCancelled else { return }
                problem = message(for: error)
                try? await Task.sleep(for: .seconds(1))
            }
            if Date().timeIntervalSince(lastStatus) > 1 {
                lastStatus = Date()
                await refreshStatus(api)
            }
            if status?.canPan == true, Date().timeIntervalSince(lastOverview) > 2 {
                lastOverview = Date()
                if let (data, _) = try? await get(api.snapshotURL(full: true, maxDim: 360)) {
                    overview = NSImage(data: data)
                }
            }
        }
    }

    /// The longer side asked of live frames: 0 in Settings means full resolution.
    private var previewSize: Int? {
        let n = UserDefaults.standard.object(forKey: Keys.previewSize) as? Int ?? Keys.defaultPreviewSize
        return n > 0 ? n : nil
    }

    private func refreshStatus(_ api: PhoneAPI) async {
        guard let (data, _) = try? await get(api.statusURL),
              let s = try? JSONDecoder().decode(PhoneStatus.self, from: data) else { return }
        if api.base == self.api?.base { status = s }
    }

    private func countFrame() {
        let now = Date()
        frameTimes.append(now)
        frameTimes.removeAll { now.timeIntervalSince($0) > 2 }
        framesPerSecond = frameTimes.count > 1
            ? Double(frameTimes.count - 1) / now.timeIntervalSince(frameTimes[0]) : 0
    }

    private func get(_ url: URL) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse else { throw ViewerError.notAnImage }
        guard http.statusCode == 200 else { throw ViewerError.http(http.statusCode) }
        return (data, http)
    }

    private func message(for error: Error) -> String {
        if let e = error as? URLError {
            switch e.code {
            case .timedOut, .cannotConnectToHost, .networkConnectionLost, .notConnectedToInternet:
                return "Can't reach \(phoneName.isEmpty ? "the phone" : phoneName). Is ScreenEye open, with the phone awake?"
            default: break
            }
        }
        return error.localizedDescription
    }

    // MARK: Camera changes

    func setZoom(_ z: Double) {
        let s = status.map { min(max(z, $0.lower), $0.maxZoom) } ?? z
        status?.zoom = s
        change(["zoom": PhoneAPI.number(s)])
    }

    func zoom(by factor: Double) {
        guard let z = status?.zoom else { return }
        setZoom(z * factor)
    }

    /// Focus (and expose) on a point in the picture, as fractions of it.
    func focus(at p: CGPoint) { change(["focus": PhoneAPI.point(p)]) }

    func setFocusLock(_ on: Bool) {
        status?.focusMode = on ? 0 : 2
        change(["lock": on ? "1" : "0"])
    }

    func setEV(_ ev: Double) {
        status?.ev = ev
        change(["ev": PhoneAPI.number(ev)])
    }

    /// Finishes a drag of the picture, drawn `drawn` points across, by `translation`.
    func pan(by translation: CGSize, drawn: CGSize) {
        guard let s = status, s.canPan else { dragOffset = .zero; return }
        let p = PictureMath.dragged(pan: s.panPoint, viewport: s.viewportRect, translation: translation, drawn: drawn)
        dragOffset = translation
        change(["pan": PhoneAPI.point(p)])
    }

    func recentre() { change(["pan": "0.5,0.5"]) }

    private func change(_ c: [String: String]) {
        generation += 1
        pending.merge(c) { $1 }
        guard !sending, let api else { return }
        sending = true
        Task {
            defer { sending = false }
            while !pending.isEmpty {
                let batch = pending
                pending = [:]
                let reply = try? await get(api.setURL(batch))
                generation += 1  // frames already on their way were taken before this change
                if let (data, _) = reply,
                   let s = try? JSONDecoder().decode(PhoneStatus.self, from: data), api.base == self.api?.base {
                    status = s
                }
            }
        }
    }

    // MARK: Pictures

    /// Takes a full-resolution picture and files it; copies it to the clipboard if asked.
    /// With `openInClaude`, also hands the saved picture to parcel-finder (note defaults to "parcel").
    func takePicture(note: String, fullFrame: Bool = false, openInClaude: Bool = false) {
        guard taken != .taking else { return }
        guard let api else { taken = .failed(ViewerError.noPhone.localizedDescription); return }
        taken = .taking
        var note = note.trimmingCharacters(in: .whitespacesAndNewlines)
        if openInClaude && note.isEmpty { note = "parcel" }
        let defaults = UserDefaults.standard
        let folder = defaults.string(forKey: Keys.folder).map { URL(filePath: $0, directoryHint: .isDirectory) }
            ?? PictureStore.defaultFolder
        Task {
            do {
                let when = Date()
                let (jpeg, http) = try await get(api.snapshotURL(full: fullFrame))
                var sidecar: [String: Any] = [
                    "taken": PictureStore.timestamp(when),
                    "note": note.isEmpty ? NSNull() : note,
                    "phone": phoneName,
                    "url": api.base.absoluteString,
                    "fullFrame": fullFrame,
                    "orientation": http.value(forHTTPHeaderField: "X-ScreenEye-Orientation") ?? NSNull(),
                    "viewportRaw": http.value(forHTTPHeaderField: "X-ScreenEye-Viewport") ?? NSNull(),
                ]
                if let (data, _) = try? await get(api.statusURL),
                   let s = try? JSONSerialization.jsonObject(with: data) {
                    sidecar["status"] = s
                }
                let url = try PictureStore.save(jpeg: jpeg, sidecar: sidecar, note: note.isEmpty ? nil : note,
                                                in: folder, at: when)
                if defaults.bool(forKey: Keys.copyAfterTaking) { Self.copy(jpeg: jpeg) }
                if openInClaude {
                    do {
                        try await ParcelFinderClient().send(savedPath: url)
                    } catch {
                        taken = .failed("Saved, but parcel-finder isn't running")
                        return
                    }
                }
                taken = .saved(url)
            } catch {
                taken = .failed(message(for: error))
            }
        }
    }

    /// Copies what's on screen (the live frame) to the clipboard.
    func copyFrame() {
        guard let frame else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.writeObjects([frame])
    }

    static func copy(jpeg: Data) {
        guard let img = NSImage(data: jpeg) else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.writeObjects([img])
    }
}
