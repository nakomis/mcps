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
                          taken: "2026-10-16T14:03:22+01:00")
        let o = try object(m)
        XCTAssertEqual(Set(o.keys), ["zoom", "lens", "ev", "focus", "lensPosition", "note", "device", "taken"])
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
        XCTAssertTrue(json.hasSuffix("\"zoom\":2}"), json)
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
