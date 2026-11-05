import XCTest
@testable import ScreenEye

/// The test vectors are iphone-remote `docs/protocol.md` §6: fob 3, counter 0x1234, 3.00 V.
final class FobProtocolTests: XCTestCase {
    private let vector = Data([0xFF, 0xFF, 0x01, 0x03, 0x34, 0x12, 0x96])

    func testDecodesTheTestVector() {
        let p = FobProtocol.decode(manufacturerData: vector)
        XCTAssertEqual(p, FobProtocol.Press(fobID: 3, counter: 0x1234, battery: 150))
        XCTAssertEqual(p?.volts ?? 0, 3.0, accuracy: 1e-9)
    }

    func testIgnoresBytesAfterThePayload() {
        XCTAssertEqual(FobProtocol.decode(manufacturerData: vector + Data([0xAA, 0xBB]))?.counter, 0x1234)
    }

    func testRejectsWhatIsNotAVersion1Press() {
        XCTAssertNil(FobProtocol.decode(manufacturerData: vector.prefix(6)))               // short
        XCTAssertNil(FobProtocol.decode(manufacturerData: Data([0x4C, 0x00]) + vector.dropFirst(2)))  // Apple's company ID
        var v2 = [UInt8](vector); v2[2] = 0x02
        XCTAssertNil(FobProtocol.decode(manufacturerData: Data(v2)))
        var id8 = [UInt8](vector); id8[3] = 8
        XCTAssertNil(FobProtocol.decode(manufacturerData: Data(id8)))
    }

    func testBatteryZeroMeansNotMeasured() {
        var b = [UInt8](vector); b[6] = 0
        XCTAssertNil(FobProtocol.decode(manufacturerData: Data(b))?.volts)
    }

    func testAckUUIDIsTheTestVector() {
        let ack = FobProtocol.ackUUID(for: .init(fobID: 3, counter: 0x1234, battery: 150))
        XCTAssertEqual(ack.uuidString, "6FDAD356-8165-4FDB-A4F2-B25C2B031234")
    }

    func testAckUUIDDoesNotDependOnBattery() {
        XCTAssertEqual(FobProtocol.ackUUID(for: .init(fobID: 7, counter: 0xFFFF, battery: 1)).uuidString,
                       "6FDAD356-8165-4FDB-A4F2-B25C2B07FFFF")
        XCTAssertEqual(FobProtocol.ackUUID(for: .init(fobID: 0, counter: 1, battery: 0)).uuidString,
                       "6FDAD356-8165-4FDB-A4F2-B25C2B000001")
    }

    func testPressUUID() {
        XCTAssertEqual(FobProtocol.pressUUID.uuidString, "3B89DBAA-2669-4548-9D04-6876798D0903")
    }
}

final class PressFilterTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_000_000)
    private func press(_ fob: UInt8, _ counter: UInt16) -> FobProtocol.Press {
        .init(fobID: fob, counter: counter, battery: 150)
    }

    func testRepeatsOfAPressAreNotNew() {
        var f = PressFilter()
        XCTAssertTrue(f.isNew(press(1, 10), at: t0))
        for i in 1...15 { XCTAssertFalse(f.isNew(press(1, 10), at: t0 + Double(i) * 0.02)) }
        XCTAssertTrue(f.isNew(press(1, 11), at: t0 + 1))
    }

    func testFobsAreSeparate() {
        var f = PressFilter()
        XCTAssertTrue(f.isNew(press(1, 10), at: t0))
        XCTAssertTrue(f.isNew(press(2, 10), at: t0))
        XCTAssertFalse(f.isNew(press(1, 10), at: t0 + 0.1))
    }

    func testTheSameCounterIsNewAfterTheTimeout() {
        var f = PressFilter()
        XCTAssertTrue(f.isNew(press(1, 10), at: t0))
        XCTAssertFalse(f.isNew(press(1, 10), at: t0 + 4.9))
        // Seen again at 4.9 s, so the 5 s runs from there.
        XCTAssertFalse(f.isNew(press(1, 10), at: t0 + 9.8))
        XCTAssertTrue(f.isNew(press(1, 10), at: t0 + 15))
    }

    func testAnyOtherCounterIsNewEvenOlder() {
        var f = PressFilter()
        XCTAssertTrue(f.isNew(press(1, 10), at: t0))
        XCTAssertTrue(f.isNew(press(1, 9), at: t0 + 0.5))  // a new cell, a random seed
    }
}

final class AckQueueTests: XCTestCase {
    private let a = UUID(), b = UUID()

    func testOneAtATimeInOrder() {
        var q = AckQueue()
        XCTAssertTrue(q.add(a)); XCTAssertTrue(q.add(b))
        XCTAssertEqual(q.next(), a)
        XCTAssertEqual(q.next(), b)
        XCTAssertNil(q.next())
        XCTAssertNil(q.current)
    }

    func testNotQueuedTwiceWhileWaitingOrOnAir() {
        var q = AckQueue()
        XCTAssertTrue(q.add(a))
        XCTAssertFalse(q.add(a))       // waiting
        _ = q.next()
        XCTAssertFalse(q.add(a))       // on air
        _ = q.next()
        XCTAssertTrue(q.add(a))        // done: a repeat of the press acks again
    }
}

final class FobNoteTests: XCTestCase {
    func testNoteIsTypedElseFobID() {
        XCTAssertEqual(FobNote.note(typed: "", fobID: 3), "fob 3")
        XCTAssertEqual(FobNote.note(typed: "  \n", fobID: 0), "fob 0")
        XCTAssertEqual(FobNote.note(typed: " bench ", fobID: 3), "bench")
    }

    func testLastPress() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        XCTAssertEqual(FobNote.lastPress(nil, now: now), "None yet")
        let seen = KeyFob.Seen(press: .init(fobID: 2, counter: 1, battery: 140), at: now - 4)
        XCTAssertEqual(FobNote.lastPress(seen, now: now), "Fob 2, 2.80 V, 4 s ago")
        let old = KeyFob.Seen(press: .init(fobID: 5, counter: 1, battery: 0), at: now - 7200)
        XCTAssertEqual(FobNote.lastPress(old, now: now), "Fob 5, 2 h ago")
    }
}
