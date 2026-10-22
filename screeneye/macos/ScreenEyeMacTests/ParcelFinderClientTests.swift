import XCTest
@testable import ScreenEye

final class ParcelFinderClientTests: XCTestCase {
    func testRequestPostsJSONPathToLoopback() throws {
        let saved = URL(filePath: "/Users/me/Pictures/ScreenEye/2026-10-22/173005-parcel.jpg")
        let req = ParcelFinderClient().request(for: saved)
        XCTAssertEqual(req.httpMethod, "POST")
        XCTAssertEqual(req.url?.absoluteString, "http://127.0.0.1:8765/photo")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let body = try JSONSerialization.jsonObject(with: try XCTUnwrap(req.httpBody)) as? [String: String]
        XCTAssertEqual(body, ["path": saved.path])
    }
}
