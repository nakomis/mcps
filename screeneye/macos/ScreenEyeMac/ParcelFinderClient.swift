import Foundation

/// Hands a saved picture to parcel-finder (nakomis/parcel-finder), the LaunchAgent on this Mac that
/// works out which Claude session bought the thing in it. ScreenEye only passes the file's path.
struct ParcelFinderClient {
    var endpoint = URL(string: "http://127.0.0.1:8765/photo")!

    func request(for savedPath: URL) -> URLRequest {
        var req = URLRequest(url: endpoint, timeoutInterval: 10)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["path": savedPath.path])
        return req
    }

    func send(savedPath: URL) async throws {
        let (_, response) = try await URLSession.shared.data(for: request(for: savedPath))
        guard let http = response as? HTTPURLResponse, http.statusCode == 202 else {
            throw URLError(.badServerResponse)
        }
    }
}
