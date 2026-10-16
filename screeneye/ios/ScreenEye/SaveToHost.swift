import Foundation
import Network
import Observation

/// Browses Bonjour for the ScreenEye host (`_screeneye-host._tcp`, TXT `path=/api/frames`).
@MainActor @Observable
final class HostFinder {
    struct Host: Equatable {
        let name: String
        let endpoint: NWEndpoint
        let path: String?
    }

    private(set) var hosts: [Host] = []
    private var browser: NWBrowser?

    func start() {
        guard browser == nil else { return }
        let b = NWBrowser(for: .bonjourWithTXTRecord(type: "_screeneye-host._tcp", domain: nil), using: .tcp)
        b.browseResultsChangedHandler = { [weak self] results, _ in
            let found: [Host] = results.compactMap { r in
                guard case let .service(name, _, _, _) = r.endpoint else { return nil }
                var path: String?
                if case let .bonjour(txt) = r.metadata { path = txt["path"] }
                return Host(name: name, endpoint: r.endpoint, path: path)
            }.sorted { $0.name < $1.name }
            Task { @MainActor in self?.hosts = found }
        }
        b.start(queue: .main)
        browser = b
    }

    /// The URL to POST to: the first host found over Bonjour, else the one typed in Settings.
    func framesURL(manual: String) async throws -> URL {
        if let host = hosts.first,
           let (address, port) = try? await Self.resolve(host.endpoint),
           let url = HostURL.frames(host: address, port: port, path: host.path) {
            return url
        }
        if let url = HostURL.frames(base: manual) { return url }
        throw SaveError.noHost
    }

    /// Resolves a Bonjour service to an IPv4 address and port by connecting to it.
    private nonisolated static func resolve(_ endpoint: NWEndpoint) async throws -> (String, Int) {
        let params = NWParameters.tcp
        if let ip = params.defaultProtocolStack.internetProtocol as? NWProtocolIP.Options {
            ip.version = .v4
        }
        let conn = NWConnection(to: endpoint, using: params)
        let once = Once()
        return try await withCheckedThrowingContinuation { cont in
            conn.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    if case let .hostPort(host, port) = conn.currentPath?.remoteEndpoint {
                        let address: String = switch host {
                        case .ipv4(let a): "\(a)".split(separator: "%").first.map(String.init) ?? "\(a)"
                        case .name(let n, _): n
                        default: "\(host)"
                        }
                        if once.claim() { cont.resume(returning: (address, Int(port.rawValue))) }
                    } else if once.claim() {
                        cont.resume(throwing: SaveError.noHost)
                    }
                    conn.cancel()
                case .failed, .waiting:
                    if once.claim() { cont.resume(throwing: SaveError.noHost) }
                    conn.cancel()
                default: break
                }
            }
            conn.start(queue: .global())
            DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
                if once.claim() { cont.resume(throwing: SaveError.noHost) }
                conn.cancel()
            }
        }
    }

    /// Lets exactly one of several racing callbacks resume a continuation.
    private final class Once: @unchecked Sendable {
        private let lock = NSLock()
        private var done = false
        func claim() -> Bool {
            lock.lock(); defer { lock.unlock() }
            if done { return false }
            done = true
            return true
        }
    }
}

/// Takes the current frame and pushes it to the host, tracking progress for the UI.
@MainActor @Observable
final class Saver {
    enum State: Equatable {
        case idle
        case saving(Double)
        case saved(String)
        case failed(String)
    }

    private(set) var state: State = .idle

    var isSaving: Bool {
        if case .saving = state { return true }
        return false
    }

    func save(camera: Camera, finder: HostFinder, note: String, manualURL: String, device: String) {
        guard !isSaving else { return }
        state = .saving(0)
        Task {
            do {
                // Take the frame first, so it's the one on screen when the button was pressed.
                let note = FrameMeta.cleanNote(note)
                let frame = await Task.detached { camera.frameForSaving(note: note, device: device) }.value
                guard let (jpeg, meta) = frame else { throw SaveError.noFrame }
                let url = try await finder.framesURL(manual: manualURL)
                let saved = try await Uploader.post(to: url, jpeg: jpeg, meta: meta) { fraction in
                    Task { @MainActor in
                        if case .saving = self.state { self.state = .saving(fraction) }
                    }
                }
                state = .saved(saved.name)
            } catch {
                state = .failed(error.localizedDescription)
            }
        }
    }
}
