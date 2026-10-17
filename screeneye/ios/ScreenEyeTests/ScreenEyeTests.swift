import XCTest
@testable import ScreenEye

final class ZoomMathTests: XCTestCase {
    func testClamp() {
        XCTAssertEqual(ZoomMath.clamp(0.2, 0.5, 60), 0.5)
        XCTAssertEqual(ZoomMath.clamp(100, 0.5, 60), 60)
        XCTAssertEqual(ZoomMath.clamp(3, 0.5, 60), 3)
    }

    func testStepsOnlyWhereTheDeviceReaches() {
        XCTAssertEqual(ZoomMath.steps(min: 0.5, max: 60), [0.5, 1, 2, 5])
        XCTAssertEqual(ZoomMath.steps(min: 1, max: 10), [1, 2, 5])
        XCTAssertEqual(ZoomMath.steps(min: 1, max: 4), [1, 2])
        XCTAssertEqual(ZoomMath.steps(min: 0.5, max: 4.999), [0.5, 1, 2, 5])  // rounding slack
    }

    func testSliderRoundTripsAndIsLogarithmic() {
        for z in [0.5, 1, 2, 7.3, 60] {
            let t = ZoomMath.sliderPosition(zoom: z, min: 0.5, max: 60)
            XCTAssertEqual(ZoomMath.zoom(sliderPosition: t, min: 0.5, max: 60), z, accuracy: 1e-9)
        }
        XCTAssertEqual(ZoomMath.sliderPosition(zoom: 2, min: 1, max: 4), 0.5, accuracy: 1e-9)
        XCTAssertEqual(ZoomMath.sliderPosition(zoom: 100, min: 1, max: 4), 1)
        XCTAssertEqual(ZoomMath.sliderPosition(zoom: 1, min: 1, max: 1), 0)
        XCTAssertEqual(ZoomMath.zoom(sliderPosition: 2, min: 1, max: 4), 4, accuracy: 1e-9)
    }

    func testLabels() {
        XCTAssertEqual(ZoomMath.label(0.5), "0.5×")
        XCTAssertEqual(ZoomMath.label(1), "1×")
        XCTAssertEqual(ZoomMath.label(2.04), "2×")
        XCTAssertEqual(ZoomMath.label(2.26), "2.3×")
        XCTAssertEqual(ZoomMath.label(12.4), "12×")
    }
}

final class FrameMetaTests: XCTestCase {
    private func object(_ m: FrameMeta) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: m.json()) as? [String: Any])
    }

    func testAllFieldsWithValues() throws {
        let m = FrameMeta(zoom: 2.5, lens: "Back Telephoto Camera", ev: -1, focus: [0.25, 0.75],
                          lensPosition: 0.8, note: "bed level", device: "iPhone",
                          taken: "2026-10-16T14:03:22+01:00", orientation: "portraitUpsideDown", rotation: 270)
        let o = try object(m)
        XCTAssertEqual(Set(o.keys), ["zoom", "lens", "ev", "focus", "lensPosition", "note", "device", "taken",
                                     "orientation", "rotation"])
        XCTAssertEqual(o["orientation"] as? String, "portraitUpsideDown")
        XCTAssertEqual(o["rotation"] as? Int, 270)
        XCTAssertEqual(o["zoom"] as? Double, 2.5)
        XCTAssertEqual(o["focus"] as? [Double], [0.25, 0.75])
        XCTAssertEqual(o["note"] as? String, "bed level")
        XCTAssertEqual(o["taken"] as? String, "2026-10-16T14:03:22+01:00")
    }

    func testAbsentValuesAreExplicitNulls() throws {
        let m = FrameMeta(zoom: 1, lens: "?", ev: 0, focus: nil, lensPosition: 0.5,
                          note: nil, device: "iPhone", taken: "2026-10-16T14:03:22+01:00")
        let json = try XCTUnwrap(String(data: m.json(), encoding: .utf8))
        XCTAssertTrue(json.contains("\"focus\":null"), json)
        XCTAssertTrue(json.contains("\"note\":null"), json)
        XCTAssertTrue(json.contains("\"orientation\":\"unknown\",\"rotation\":0"), json)  // the defaults
    }

    func testCleanNote() {
        XCTAssertNil(FrameMeta.cleanNote("   \n"))
        XCTAssertEqual(FrameMeta.cleanNote("  first layer "), "first layer")
    }

    func testRoundingRemovesFloatNoise() throws {
        XCTAssertEqual(FrameMeta.round(Double(Float(0.3)), 2), 0.3)
        let m = FrameMeta(zoom: FrameMeta.round(2.0000001, 2), lens: "x", ev: FrameMeta.round(Double(Float(0.3)), 2),
                          focus: nil, lensPosition: 0, note: nil, device: "d", taken: "t")
        let json = try XCTUnwrap(String(data: m.json(), encoding: .utf8))
        XCTAssertTrue(json.contains("\"ev\":0.3,"), json)
        XCTAssertTrue(json.contains("\"zoom\":2}") || json.contains("\"zoom\":2,"), json)
    }

    func testTimestampAlwaysHasANumericOffset() {
        let date = Date(timeIntervalSince1970: 1_792_155_802)  // 2026-10-16T13:03:22Z
        XCTAssertEqual(FrameMeta.timestamp(date, timeZone: TimeZone(identifier: "Europe/London")!),
                       "2026-10-16T14:03:22+01:00")
        XCTAssertEqual(FrameMeta.timestamp(date, timeZone: TimeZone(identifier: "UTC")!),
                       "2026-10-16T13:03:22+00:00")
    }
}

final class MultipartTests: XCTestCase {
    func testBody() {
        let form = Multipart(boundary: "B")
        XCTAssertEqual(form.contentType, "multipart/form-data; boundary=B")
        let body = form.body(jpeg: Data([0xFF, 0xD8, 0xFF]), meta: Data("{\"zoom\":1}".utf8))
        var expected = Data("--B\r\nContent-Disposition: form-data; name=\"image\"; filename=\"frame.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".utf8)
        expected.append(Data([0xFF, 0xD8, 0xFF]))
        expected.append(Data("\r\n--B\r\nContent-Disposition: form-data; name=\"meta\"\r\n\r\n{\"zoom\":1}\r\n--B--\r\n".utf8))
        XCTAssertEqual(body, expected)
    }

    func testDefaultBoundariesDiffer() {
        XCTAssertNotEqual(Multipart().boundary, Multipart().boundary)
    }
}

final class HostURLTests: XCTestCase {
    func testTypedAddresses() {
        XCTAssertEqual(HostURL.frames(base: "mace.local:8765")?.absoluteString, "http://mace.local:8765/api/frames")
        XCTAssertEqual(HostURL.frames(base: " http://172.29.0.10:8765/ ")?.absoluteString, "http://172.29.0.10:8765/api/frames")
        XCTAssertEqual(HostURL.frames(base: "http://mace:8765/api/frames")?.absoluteString, "http://mace:8765/api/frames")
        XCTAssertEqual(HostURL.frames(base: "https://mace.example/eye")?.absoluteString, "https://mace.example/eye/api/frames")
        XCTAssertNil(HostURL.frames(base: ""))
        XCTAssertNil(HostURL.frames(base: "ftp://mace"))
    }

    func testBonjourHost() {
        XCTAssertEqual(HostURL.frames(host: "172.29.0.10", port: 8765, path: "/api/frames")?.absoluteString,
                       "http://172.29.0.10:8765/api/frames")
        XCTAssertEqual(HostURL.frames(host: "172.29.0.10", port: 8765, path: nil)?.absoluteString,
                       "http://172.29.0.10:8765/api/frames")
        XCTAssertEqual(HostURL.frames(host: "mace.local", port: 80, path: "frames")?.absoluteString,
                       "http://mace.local:80/frames")
    }
}

final class SaveErrorTests: XCTestCase {
    func testHostErrorMessageIsUsed() {
        XCTAssertEqual(SaveError.from(status: 400, body: Data("{\"error\": \"no image\"}".utf8)),
                       .server(status: 400, message: "no image"))
    }

    func testPlainBodyOrStatusTextOtherwise() {
        XCTAssertEqual(SaveError.from(status: 502, body: Data("Bad Gateway\n".utf8)),
                       .server(status: 502, message: "Bad Gateway"))
        if case let .server(status, message) = SaveError.from(status: 500, body: Data()) {
            XCTAssertEqual(status, 500)
            XCTAssertFalse(message.isEmpty)
        } else {
            XCTFail("expected a server error")
        }
    }

    func testSavedFrameDecodes() throws {
        let json = Data(#"{"name": "140322-bed-level.jpg", "path": "screeneye/2026-10-16/140322-bed-level.jpg"}"#.utf8)
        let saved = try JSONDecoder().decode(SavedFrame.self, from: json)
        XCTAssertEqual(saved, SavedFrame(name: "140322-bed-level.jpg", path: "screeneye/2026-10-16/140322-bed-level.jpg"))
    }
}

final class OrientationTests: XCTestCase {
    private func state(_ gx: Double, _ gy: Double, _ gz: Double,
                       from previous: OrientationState = OrientationState()) -> OrientationState {
        OrientationMath.update(previous, gx: gx, gy: gy, gz: gz)
    }

    func testUprightOrientations() {
        // CoreMotion's device frame: x to the screen's right, y to its top, z out of the screen.
        XCTAssertEqual(state(0, -1, 0), OrientationState(orientation: .portrait, rotation: 90))
        XCTAssertEqual(state(0, 1, 0), OrientationState(orientation: .portraitUpsideDown, rotation: 270))
        XCTAssertEqual(state(-1, 0, 0), OrientationState(orientation: .landscapeLeft, rotation: 0))
        XCTAssertEqual(state(1, 0, 0), OrientationState(orientation: .landscapeRight, rotation: 180))
    }

    func testTiltedTowardsTheCameraStillCounts() {
        // Leaning back 45° (looking down at a desk), still in landscape right.
        XCTAssertEqual(state(0.7, 0.05, -0.7).orientation, .landscapeRight)
        XCTAssertEqual(state(0.1, -0.6, 0.79).orientation, .portrait)
    }

    func testFlatKeepsTheLastRotation() {
        let upsideDown = state(0, 1, 0)
        XCTAssertEqual(state(0.1, 0.2, -0.97, from: upsideDown), OrientationState(orientation: .faceUp, rotation: 270))
        XCTAssertEqual(state(0.1, 0.2, 0.97, from: upsideDown), OrientationState(orientation: .faceDown, rotation: 270))
        // And rotation picks up again when it stands back up.
        let flat = state(0, 0, -1, from: upsideDown)
        XCTAssertEqual(state(1, 0, 0, from: flat), OrientationState(orientation: .landscapeRight, rotation: 180))
    }

    func testFlatFromTheStartIsRaw() {
        XCTAssertEqual(state(0, 0, -1), OrientationState(orientation: .faceUp, rotation: 0))
        XCTAssertEqual(state(0, 0, 1), OrientationState(orientation: .faceDown, rotation: 0))
    }

    func testNoReadingChangesNothing() {
        let p = state(1, 0, 0)
        XCTAssertEqual(state(0, 0, 0, from: p), p)
        XCTAssertEqual(state(.nan, 0, -1, from: p), p)
    }

    func testHysteresisNearTheDiagonal() {
        let left = state(-1, 0, 0)  // landscapeLeft, 0°
        func at(_ deg: Double, from p: OrientationState) -> OrientationState {
            // Gravity for a picture angle `deg` (0 = landscapeLeft, clockwise).
            let a = (deg - 180) * .pi / 180
            return state(cos(a), sin(a), 0, from: p)
        }
        XCTAssertEqual(at(50, from: left), left)                     // past 45°, inside the dead band
        XCTAssertEqual(at(56, from: left).orientation, .portrait)    // out of it
        XCTAssertEqual(at(50, from: OrientationState()).orientation, .portrait)  // no previous: nearest
        XCTAssertEqual(at(-50, from: left), left)
        XCTAssertEqual(at(-56, from: left).orientation, .portraitUpsideDown)
        // Standing up from flat near the diagonal keeps the rotation from before it lay down.
        let flat = state(0, 0, -1, from: left)
        XCTAssertEqual(at(50, from: flat), left)
    }

    func testEveryAngleSnapsToAQuarterTurn() {
        for deg in stride(from: 0.0, to: 360, by: 7.5) {
            let a = (deg - 180) * .pi / 180
            let s = state(cos(a), sin(a), 0)
            XCTAssertTrue([0, 90, 180, 270].contains(s.rotation), "\(deg)")
            XCTAssertEqual(s.orientation.ownRotation, s.rotation)
        }
    }

    /// The `CIImage` turn really is clockwise: a 2×1 image, red left, blue right, turned 90°
    /// clockwise has red on top.
    func testImageOrientationTurnsClockwise() throws {
        let red = CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 1, height: 1))
        let blue = CIImage(color: .blue).cropped(to: CGRect(x: 1, y: 0, width: 1, height: 1))
        let img = red.composited(over: blue)
        func pixels(_ rotation: Int) throws -> (Int, [String]) {
            let out = img.oriented(OrientationMath.imageOrientation(rotation: rotation))
            let cg = try XCTUnwrap(CIContext().createCGImage(out, from: out.extent))
            var data = [UInt8](repeating: 0, count: cg.width * cg.height * 4)
            let ctx = try XCTUnwrap(CGContext(data: &data, width: cg.width, height: cg.height, bitsPerComponent: 8,
                                              bytesPerRow: cg.width * 4, space: CGColorSpaceCreateDeviceRGB(),
                                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            ctx.draw(cg, in: CGRect(x: 0, y: 0, width: cg.width, height: cg.height))
            // Rows top first, as the picture is seen.
            return (cg.width, stride(from: 0, to: data.count, by: 4).map {
                data[$0] > 128 ? "R" : data[$0 + 2] > 128 ? "B" : "?"
            })
        }
        func check(_ rotation: Int, width: Int, _ want: [String]) throws {
            let (w, got) = try pixels(rotation)
            XCTAssertEqual(w, width, "\(rotation)")
            XCTAssertEqual(got, want, "\(rotation)")
        }
        try check(0, width: 2, ["R", "B"])
        try check(90, width: 1, ["R", "B"])   // red on top
        try check(180, width: 2, ["B", "R"])
        try check(270, width: 1, ["B", "R"])  // red at the bottom
    }
}
