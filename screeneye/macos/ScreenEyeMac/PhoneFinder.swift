import Foundation
import Network
import Observation

/// Browses Bonjour for phones running ScreenEye (`_screeneye._tcp`).
@MainActor @Observable
final class PhoneFinder {
    struct Phone: Equatable, Identifiable {
        let name: String
        let endpoint: NWEndpoint
        var id: String { name }
    }

    private(set) var phones: [Phone] = []
    /// Why browsing stopped, if it did (e.g. Local Network access was refused).
    private(set) var problem: String?
    private var browser: NWBrowser?

    func start() {
        guard browser == nil else { return }
        let b = NWBrowser(for: .bonjour(type: "_screeneye._tcp", domain: nil), using: .tcp)
        b.browseResultsChangedHandler = { [weak self] results, _ in
            var seen = Set<String>()  // one result per interface: keep the first of each name
            let found: [Phone] = results.compactMap { r in
                guard case let .service(name, _, _, _) = r.endpoint, seen.insert(name).inserted else { return nil }
                return Phone(name: name, endpoint: r.endpoint)
            }.sorted { $0.name < $1.name }
            Task { @MainActor in self?.phones = found }
        }
        b.stateUpdateHandler = { [weak self] state in
            let problem: String? = switch state {
            case .failed(let e), .waiting(let e): "Bonjour: \(e.localizedDescription)"
            default: nil
            }
            Task { @MainActor in self?.problem = problem }
        }
        b.start(queue: .main)
        browser = b
    }

    /// The phone's base URL: its IPv4 address and port, found by connecting to the service.
    nonisolated static func resolve(_ endpoint: NWEndpoint) async throws -> URL {
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
                        if let url = PhoneAPI.base(host: address, port: Int(port.rawValue)), once.claim() {
                            cont.resume(returning: url)
                        }
                    }
                    if once.claim() { cont.resume(throwing: ViewerError.notFound) }
                    conn.cancel()
                case .failed:  // .waiting may recover (the Local Network prompt): leave it to the timeout
                    if once.claim() { cont.resume(throwing: ViewerError.notFound) }
                    conn.cancel()
                default: break
                }
            }
            conn.start(queue: .global())
            DispatchQueue.global().asyncAfter(deadline: .now() + 8) {
                if once.claim() { cont.resume(throwing: ViewerError.notFound) }
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
