import XCTest
@testable import ScreenEye

final class ParcelFinderTests: XCTestCase {
    func testPairingTokenFromTheQRCodeOrPasted() {
        XCTAssertEqual(ParcelPairing.token(from: #"{"parcel_finder_token": "abc-DEF_123"}"#), "abc-DEF_123")
        XCTAssertEqual(ParcelPairing.token(from: "  abc-DEF_123456789012345678\n"), "abc-DEF_123456789012345678")
        XCTAssertNil(ParcelPairing.token(from: "https://example.com/not-a-token"))
        XCTAssertNil(ParcelPairing.token(from: "short"))
        XCTAssertNil(ParcelPairing.token(from: #"{"other": "x"}"#))
    }

    func testOCRKeepsConfidentLinesInOrder() {
        let lines: [(String, Float)] = [("SINT-100 FRONT jaw pins 1-120", 1.0), ("ĘhñЛůñû", 0.3),
                                        ("Designed by Nakomis/C", 0.95), ("  ", 1.0)]
        XCTAssertEqual(ParcelOCR.text(lines: lines), "SINT-100 FRONT jaw pins 1-120\nDesigned by Nakomis/C")
        XCTAssertNil(ParcelOCR.text(lines: [("junk", 0.2)]))
    }

    func testRunRequestBody() throws {
        let body = try ParcelClient.runBody(jpeg: Data([0xFF, 0xD8, 0xFF]), ocr: "Core2350B")
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: body) as? [String: Any])
        XCTAssertEqual(json["photo"] as? String, "/9j/")
        XCTAssertEqual(json["ocr"] as? String, "Core2350B")
        let none = try XCTUnwrap(JSONSerialization.jsonObject(with: try ParcelClient.runBody(jpeg: Data(), ocr: nil)) as? [String: Any])
        XCTAssertTrue(none["ocr"] is NSNull)
    }

    func testRunStatusDecodes() throws {
        let data = Data(#"""
        {"run_id": "20261023-150000-abcd", "state": "waiting", "step": "Which conversation was the board bought in?",
         "item": "Core2350B board", "session": "mace", "outcome": null, "detail": "",
         "choices": [{"session_id": "sid-a", "title": "mace", "when": "10 Oct 2026", "hits": 12}]}
        """#.utf8)
        let s = try JSONDecoder().decode(ParcelRun.self, from: data)
        XCTAssertEqual(s.state, .waiting)
        XCTAssertEqual(s.choices.first?.title, "mace")
        XCTAssertFalse(s.isFinished)
    }

    func testOutcomeWording() {
        func run(_ outcome: String, session: String? = "ddr-banging", detail: String = "") -> ParcelRun {
            ParcelRun(runID: "r", state: .done, step: "", item: "board", session: session, outcome: outcome,
                      detail: detail, choices: [])
        }
        XCTAssertEqual(run("delivered").summary, "Sent to ddr-banging")
        XCTAssertEqual(run("resumed").summary, "Opened ddr-banging")
        XCTAssertEqual(run("cancelled", session: nil).summary, "Cancelled")
        XCTAssertEqual(run("not-found", session: nil).summary, "Couldn't find the conversation for the board")
        XCTAssertTrue(run("error", session: nil, detail: "boom").summary.contains("boom"))
        XCTAssertTrue(run("delivered").succeeded)
        XCTAssertFalse(run("error").succeeded)
    }

    func testBaseURL() {
        XCTAssertEqual(HostURL.base(host: "192.168.1.5", port: 8776)?.absoluteString, "http://192.168.1.5:8776")
    }
}
