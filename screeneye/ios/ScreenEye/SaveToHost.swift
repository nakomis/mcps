import Foundation
import Network
import Observation

/// Browses Bonjour for the ScreenEye host (`_screeneye-host._tcp`, TXT `path=/api/frames`), or
/// another service type (parcel-finder's `_parcel-finder._tcp`).
@MainActor @Observable
final class HostFinder {
    struct Host: Equatable {
        let name: String
        let endpoint: NWEndpoint
        let path: String?
    }

    private(set) var hosts: [Host] = []
    private var browser: NWBrowser?
    let type: String

    init(type: String = "_screeneye-host._tcp") {
        self.type = type
    }

    func start() {
        guard browser == nil else { return }
        let b = NWBrowser(for: .bonjourWithTXTRecord(type: type, domain: nil), using: .tcp)
        b.browseResultsChangedHandler = { [weak self] results, _ in
            var seen = Set<String>()  // one result per interface: keep the first of each name
            let found: [Host] = results.compactMap { r in
                guard case let .service(name, _, _, _) = r.endpoint, seen.insert(name).inserted else { return nil }
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

    /// `http://address:port` of the first service found over Bonjour.
    func baseURL() async throws -> URL {
        guard let host = hosts.first else { throw SaveError.noHost }
        let (address, port) = try await Self.resolve(host.endpoint)
        guard let url = HostURL.base(host: address, port: port) else { throw SaveError.noHost }
        return url
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
                case .failed:  // .waiting may recover (Mac waking, permission prompt): leave it to the timeout
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
                        // These tasks can run out of order: never move the bar backwards.
                        if case .saving(let shown) = self.state, fraction > shown { self.state = .saving(fraction) }
                    }
                }
                state = .saved(saved.name)
            } catch let error as URLError where error.code == .appTransportSecurityRequiresSecureConnection {
                state = .failed("Plain http only works for local hosts (.local names or IP addresses). Use https, or a local address.")
            } catch {
                state = .failed(error.localizedDescription)
            }
        }
    }
}
