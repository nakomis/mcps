import XCTest
@testable import ScreenEye

final class PhoneAPITests: XCTestCase {
    func testBaseFromTyped() {
        XCTAssertEqual(PhoneAPI.base(from: "172.29.0.40:8080")?.absoluteString, "http://172.29.0.40:8080")
        XCTAssertEqual(PhoneAPI.base(from: " http://iphone.local:9000/ ")?.absoluteString, "http://iphone.local:9000")
        XCTAssertEqual(PhoneAPI.base(from: "172.29.0.40")?.absoluteString, "http://172.29.0.40:8080")  // default port
        XCTAssertEqual(PhoneAPI.base(from: "http://172.29.0.40:8080/status?x=1")?.absoluteString, "http://172.29.0.40:8080")
        XCTAssertNil(PhoneAPI.base(from: ""))
        XCTAssertNil(PhoneAPI.base(from: "https://172.29.0.40"))  // the phone serves plain http
        XCTAssertNil(PhoneAPI.base(from: "http://"))
    }

    func testURLsAreUpright() {
        let api = PhoneAPI(base: URL(string: "http://10.0.0.2:8080")!)
        XCTAssertEqual(api.snapshotURL().absoluteString, "http://10.0.0.2:8080/snapshot.jpg?upright=1")
        XCTAssertEqual(api.snapshotURL(full: true, maxDim: 360).absoluteString,
                       "http://10.0.0.2:8080/snapshot.jpg?upright=1&full=1&max=360")
        XCTAssertEqual(api.statusURL.absoluteString, "http://10.0.0.2:8080/status?upright=1")
        XCTAssertEqual(api.setURL(["zoom": "2", "pan": "0.3,0.6"]).absoluteString,
                       "http://10.0.0.2:8080/set?pan=0.3,0.6&zoom=2&upright=1")
    }

    func testNumbers() {
        XCTAssertEqual(PhoneAPI.number(2), "2")
        XCTAssertEqual(PhoneAPI.number(2.5), "2.5")
        XCTAssertEqual(PhoneAPI.number(0.333333), "0.3333")
        XCTAssertEqual(PhoneAPI.number(-1), "-1")
        XCTAssertEqual(PhoneAPI.point(CGPoint(x: 0.25, y: 1)), "0.25,1")
    }
}

final class PhoneStatusTests: XCTestCase {
    func testDecodesTheCurrentApp() throws {
        let json = """
        {"coordinates":"upright","ev":-1,"focusMode":0,"lens":"Back Camera","lensPosition":0.5,
         "lensSteps":[0.5,1,2,4],"maxZoom":15,"minZoom":0.5,"opticalZoom":2,"orientation":"portrait",
         "pan":[0.4,0.6],"rotation":90,"viewport":[0.15,0.35,0.5,0.5],"zoom":4}
        """
        let s = try JSONDecoder().decode(PhoneStatus.self, from: Data(json.utf8))
        XCTAssertEqual(s.lower, 0.5)
        XCTAssertEqual(s.lensSteps, [0.5, 1, 2, 4])
        XCTAssertEqual(s.digital, 2)
        XCTAssertTrue(s.canPan)
        XCTAssertTrue(s.focusLocked)
        XCTAssertEqual(s.panPoint, CGPoint(x: 0.4, y: 0.6))
        XCTAssertEqual(s.viewportRect, CGRect(x: 0.15, y: 0.35, width: 0.5, height: 0.5))
    }

    func testDecodesAnOldApp() throws {
        // Before EYE-3/4/5: no orientation, pan, viewport, minZoom or lens steps.
        let json = #"{"zoom":1,"maxZoom":10,"lens":"Back Camera","focusMode":2,"lensPosition":0.4,"ev":0}"#
        let s = try JSONDecoder().decode(PhoneStatus.self, from: Data(json.utf8))
        XCTAssertEqual(s.lower, 1)
        XCTAssertEqual(s.digital, 1)
        XCTAssertFalse(s.canPan)
        XCTAssertFalse(s.focusLocked)
        XCTAssertEqual(s.panPoint, CGPoint(x: 0.5, y: 0.5))
        XCTAssertEqual(s.viewportRect, CGRect(x: 0, y: 0, width: 1, height: 1))
    }
}

final class PhoneChoiceTests: XCTestCase {
    func testTheOneShowingIsTriedFirst() {
        XCTAssertEqual(PhoneChoice.order(["iPad", "iPhone", "Old"], current: "iPhone"), ["iPhone", "Old", "iPad"])
        XCTAssertEqual(PhoneChoice.order(["iPad", "iPhone"], current: "Gone"), ["iPad", "iPhone"])
        XCTAssertEqual(PhoneChoice.order([], current: "iPhone"), [])
    }

    func testMovesOnOnlyWhenQuietAndThereIsAnother() {
        XCTAssertTrue(PhoneChoice.shouldMoveOn(silentFor: 6, phones: 2))
        XCTAssertFalse(PhoneChoice.shouldMoveOn(silentFor: 5.9, phones: 2))
        XCTAssertFalse(PhoneChoice.shouldMoveOn(silentFor: 60, phones: 1))  // nowhere to go: keep retrying
    }
}

final class PictureMathTests: XCTestCase {
    func testFitted() {
        // A landscape picture in a taller view: full width, centred vertically.
        XCTAssertEqual(PictureMath.fitted(CGSize(width: 1600, height: 900), in: CGSize(width: 800, height: 600)),
                       CGRect(x: 0, y: 75, width: 800, height: 450))
        // A portrait one: full height, centred horizontally.
        XCTAssertEqual(PictureMath.fitted(CGSize(width: 900, height: 1600), in: CGSize(width: 800, height: 800)),
                       CGRect(x: 175, y: 0, width: 450, height: 800))
        XCTAssertEqual(PictureMath.fitted(.zero, in: CGSize(width: 10, height: 10)),
                       CGRect(x: 0, y: 0, width: 10, height: 10))
    }

    func testFraction() {
        let r = CGRect(x: 0, y: 75, width: 800, height: 450)
        XCTAssertEqual(PictureMath.fraction(of: CGPoint(x: 400, y: 300), in: r), CGPoint(x: 0.5, y: 0.5))
        XCTAssertEqual(PictureMath.fraction(of: CGPoint(x: 0, y: 75), in: r), CGPoint(x: 0, y: 0))
        XCTAssertNil(PictureMath.fraction(of: CGPoint(x: 400, y: 10), in: r))  // in the letterbox
    }

    func testDragMovesTheCentreTheOtherWay() {
        let vp = CGRect(x: 0.25, y: 0.25, width: 0.5, height: 0.5)
        // Dragging the picture right by a quarter of its width shows what was to the left:
        // the centre moves left by a quarter of the viewport.
        let p = PictureMath.dragged(pan: CGPoint(x: 0.5, y: 0.5), viewport: vp,
                                    translation: CGSize(width: 200, height: -100), drawn: CGSize(width: 800, height: 400))
        XCTAssertEqual(p.x, 0.375, accuracy: 1e-9)
        XCTAssertEqual(p.y, 0.625, accuracy: 1e-9)
    }

    func testDragStaysInsideTheFrame() {
        let vp = CGRect(x: 0.25, y: 0.25, width: 0.5, height: 0.5)
        let p = PictureMath.dragged(pan: CGPoint(x: 0.5, y: 0.5), viewport: vp,
                                    translation: CGSize(width: 10_000, height: -10_000), drawn: CGSize(width: 800, height: 400))
        XCTAssertEqual(p, CGPoint(x: 0.25, y: 0.75))
    }
}

final class PictureStoreTests: XCTestCase {
    private var folder: URL!

    override func setUpWithError() throws {
        folder = FileManager.default.temporaryDirectory.appending(path: "screeneye-tests-\(UUID().uuidString)")
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: folder)
    }

    func testSlugMatchesTheHost() {
        XCTAssertEqual(PictureStore.slug("BIOS setup — page 2!"), "bios-setup-page-2")
        XCTAssertEqual(PictureStore.slug("Café crème"), "cafe-creme")
        XCTAssertEqual(PictureStore.slug("  --  "), "")
        XCTAssertEqual(PictureStore.slug(nil), "")
        XCTAssertEqual(PictureStore.slug(String(repeating: "ab-", count: 40)).count, 59)  // 60, less the trailing dash
    }

    func testStamp() {
        let date = Date(timeIntervalSince1970: 1_790_928_000)  // 2026-10-02 08:00:00 UTC
        let (day, time) = PictureStore.stamp(date, timeZone: TimeZone(identifier: "Europe/London")!)
        XCTAssertEqual(day, "2026-10-02")
        XCTAssertEqual(time, "090000")
        XCTAssertEqual(PictureStore.timestamp(date, timeZone: TimeZone(identifier: "Europe/London")!),
                       "2026-10-02T09:00:00+01:00")
    }

    func testSavesBesideASidecarAndNeverOverwrites() throws {
        let date = Date()
        let (day, time) = PictureStore.stamp(date)
        let a = try PictureStore.save(jpeg: Data([0xFF, 0xD8, 0xFF, 1]), sidecar: ["note": "x"], note: "BIOS", in: folder, at: date)
        let b = try PictureStore.save(jpeg: Data([0xFF, 0xD8, 0xFF, 2]), sidecar: [:], note: "BIOS", in: folder, at: date)
        let c = try PictureStore.save(jpeg: Data([0xFF, 0xD8, 0xFF, 3]), sidecar: [:], note: nil, in: folder, at: date)
        XCTAssertEqual(a.path, folder.appending(path: "\(day)/\(time)-bios.jpg").path)
        XCTAssertEqual(b.lastPathComponent, "\(time)-bios-2.jpg")
        XCTAssertEqual(c.lastPathComponent, "\(time).jpg")
        XCTAssertEqual(try Data(contentsOf: a), Data([0xFF, 0xD8, 0xFF, 1]))
        let meta = try JSONSerialization.jsonObject(
            with: Data(contentsOf: a.deletingPathExtension().appendingPathExtension("json"))) as? [String: Any]
        XCTAssertEqual(meta?["name"] as? String, "\(time)-bios.jpg")
        XCTAssertEqual(meta?["bytes"] as? Int, 4)
        XCTAssertEqual(meta?["note"] as? String, "x")
    }
}
