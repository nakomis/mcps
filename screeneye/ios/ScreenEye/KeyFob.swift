import CoreBluetooth
import Foundation
import Observation

/// The iphone-remote fob's over-the-air protocol, version 1 (nakomis/iphone-remote
/// `docs/protocol.md`, IREM-2). A press is an advert from the fob; the ack is an advert from the
/// phone, a 128-bit service UUID that carries the fob's ID and the press's counter.
enum FobProtocol {
    /// The service UUID every press advert carries.
    static let pressUUID = UUID(uuidString: "3B89DBAA-2669-4548-9D04-6876798D0903")!
    /// The first 13 bytes of every ack UUID, in string order.
    static let ackPrefix: [UInt8] = [0x6F, 0xDA, 0xD3, 0x56, 0x81, 0x65, 0x4F, 0xDB, 0xA4, 0xF2, 0xB2, 0x5C, 0x2B]
    static let companyID: UInt16 = 0xFFFF
    static let version: UInt8 = 0x01
    static let maxFobID: UInt8 = 7

    struct Press: Equatable, Sendable {
        let fobID: UInt8
        let counter: UInt16
        /// VDD in units of 20 mV; 0 when the fob didn't measure it.
        let battery: UInt8

        /// The cell's voltage, or nil when the fob didn't measure it.
        var volts: Double? { battery == 0 ? nil : Double(battery) * 0.02 }
    }

    /// Decodes CoreBluetooth's manufacturer data (company ID first, little-endian), or nil when
    /// it isn't a version-1 press. Bytes after the payload are ignored, for later versions.
    static func decode(manufacturerData data: Data) -> Press? {
        let b = [UInt8](data)
        guard b.count >= 7,
              UInt16(b[0]) | UInt16(b[1]) << 8 == companyID,
              b[2] == version,
              b[3] <= maxFobID else { return nil }
        return Press(fobID: b[3], counter: UInt16(b[4]) | UInt16(b[5]) << 8, battery: b[6])
    }

    /// The ack for a press: the prefix, then the fob ID, then the counter, most significant
    /// byte first (in string order; on air the bytes are reversed and the counter little-endian).
    static func ackUUID(for press: Press) -> UUID {
        let b = ackPrefix + [press.fobID, UInt8(press.counter >> 8), UInt8(press.counter & 0xFF)]
        return UUID(uuid: (b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
                           b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]))
    }
}

/// De-duplicates presses: the fob repeats each one about 15 times. A press is new when its
/// counter differs from the last one seen from that fob, or when nothing has been heard from
/// that fob for longer than `timeout` (a fob with a new cell can draw its old counter again).
struct PressFilter {
    var timeout: TimeInterval = 5
    private var last: [UInt8: (counter: UInt16, seen: Date)] = [:]

    mutating func isNew(_ press: FobProtocol.Press, at now: Date) -> Bool {
        defer { last[press.fobID] = (press.counter, now) }
        guard let prev = last[press.fobID] else { return true }
        return prev.counter != press.counter || now.timeIntervalSince(prev.seen) > timeout
    }
}

/// The acks waiting to be advertised, one at a time. An ack already waiting or on air isn't
/// queued twice; once it has been on air, a repeat of the press queues it again (the fob may
/// have missed it).
struct AckQueue {
    private(set) var current: UUID?
    private(set) var waiting: [UUID] = []

    /// Queues an ack; true if it was added.
    @discardableResult mutating func add(_ ack: UUID) -> Bool {
        guard ack != current, !waiting.contains(ack) else { return false }
        waiting.append(ack)
        return true
    }

    /// Ends the ack on air (if any) and returns the next one to advertise.
    mutating func next() -> UUID? {
        current = waiting.isEmpty ? nil : waiting.removeFirst()
        return current
    }
}

/// Listens for iphone-remote fob presses while the app is on screen, acks each one, then tells
/// `onPress` about each new press. The ack goes first, so its timing doesn't depend on what
/// the press does.
@MainActor @Observable
final class KeyFob: NSObject {
    /// How long each ack is advertised.
    static let ackDuration: Duration = .milliseconds(150)

    struct Seen: Equatable {
        let press: FobProtocol.Press
        let at: Date
    }

    enum Radio: Equatable { case starting, on, off, unauthorised, unsupported }

    /// The last press acted on.
    private(set) var lastPress: Seen?
    private(set) var radio: Radio = .starting
    /// Told about each new press, after its ack has been queued.
    var onPress: (FobProtocol.Press) -> Void = { _ in }

    private var enabled = false
    private var central: CBCentralManager?
    private var peripheral: CBPeripheralManager?
    private var filter = PressFilter()
    private var acks = AckQueue()
    private var acking: Task<Void, Never>?

    /// Turns listening on or off. The Bluetooth managers (and so iOS's permission prompt) are
    /// only created the first time it's turned on.
    func setEnabled(_ on: Bool) {
        enabled = on
        if on, central == nil {
            central = CBCentralManager(delegate: self, queue: .main)
            peripheral = CBPeripheralManager(delegate: self, queue: .main)
        }
        updateScan()
    }

    private func updateScan() {
        guard let central else { return }
        if enabled, central.state == .poweredOn {
            central.scanForPeripherals(withServices: [CBUUID(nsuuid: FobProtocol.pressUUID)],
                                       options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
        } else if central.state == .poweredOn {
            central.stopScan()
        }
    }

    fileprivate func heard(_ press: FobProtocol.Press, at now: Date = Date()) {
        let new = filter.isNew(press, at: now)
        if acks.add(FobProtocol.ackUUID(for: press)) { advertiseAcks() }
        if new {
            lastPress = Seen(press: press, at: now)
            onPress(press)
        }
    }

    /// Advertises the queued acks in turn, each for `ackDuration`.
    private func advertiseAcks() {
        guard acking == nil, let peripheral, peripheral.state == .poweredOn else { return }
        acking = Task { @MainActor in
            while let ack = acks.next() {
                // No local name and exactly one UUID, so it fits the primary advert packet.
                peripheral.startAdvertising([CBAdvertisementDataServiceUUIDsKey: [CBUUID(nsuuid: ack)]])
                try? await Task.sleep(for: Self.ackDuration)
                peripheral.stopAdvertising()
            }
            acking = nil
        }
    }

    fileprivate func radioChanged(_ state: CBManagerState) {
        radio = switch state {
        case .poweredOn: .on
        case .poweredOff, .resetting: .off
        case .unauthorized: .unauthorised
        case .unsupported: .unsupported
        default: .starting
        }
        updateScan()
    }
}

extension KeyFob: CBCentralManagerDelegate {
    nonisolated func centralManagerDidUpdateState(_ central: CBCentralManager) {
        let state = central.state
        MainActor.assumeIsolated { radioChanged(state) }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                                    advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard let data = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data,
              let press = FobProtocol.decode(manufacturerData: data) else { return }
        MainActor.assumeIsolated { heard(press) }
    }
}

extension KeyFob: CBPeripheralManagerDelegate {
    nonisolated func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
        let on = peripheral.state == .poweredOn
        MainActor.assumeIsolated { if on { advertiseAcks() } }
    }
}

/// Text for the key fob: the note a press saves with, and Settings' status lines.
enum FobNote {
    static func note(typed: String, fobID: UInt8) -> String {
        FrameMeta.cleanNote(typed) ?? "fob \(fobID)"
    }

    static func radio(_ radio: KeyFob.Radio) -> String {
        switch radio {
        case .starting: "Starting…"
        case .on: "On"
        case .off: "Off"
        case .unauthorised: "Not allowed (Settings → ScreenEye → Bluetooth)"
        case .unsupported: "Not supported"
        }
    }

    static func lastPress(_ seen: KeyFob.Seen?, now: Date) -> String {
        guard let seen else { return "None yet" }
        let ago = Int(now.timeIntervalSince(seen.at).rounded())
        let when = ago < 60 ? "\(ago) s ago" : ago < 3600 ? "\(ago / 60) min ago" : "\(ago / 3600) h ago"
        let volts = seen.press.volts.map { String(format: ", %.2f V", $0) } ?? ""
        return "Fob \(seen.press.fobID)\(volts), \(when)"
    }
}
