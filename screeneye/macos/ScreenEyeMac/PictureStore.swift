import Foundation

/// Files pictures as the ScreenEye host does: `<folder>/YYYY-MM-DD/HHMMSS[-note].jpg`, with a
/// `.json` sidecar beside it, never overwriting (`-2`, `-3`, ...).
enum PictureStore {
    static let slugMax = 60

    /// The default folder: `~/Pictures/ScreenEye`.
    static var defaultFolder: URL {
        FileManager.default.homeDirectoryForCurrentUser.appending(path: "Pictures/ScreenEye", directoryHint: .isDirectory)
    }

    /// A filename-safe, lower-case, ASCII form of the note ("" if nothing is left).
    static func slug(_ note: String?) -> String {
        guard let note else { return "" }
        let ascii = note.applyingTransform(.toLatin, reverse: false)?
            .applyingTransform(.stripDiacritics, reverse: false) ?? note
        let s = ascii.lowercased()
            .replacingOccurrences(of: "[^a-z0-9]+", with: "-", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        var cut = String(s.prefix(slugMax))
        while cut.hasSuffix("-") { cut.removeLast() }
        return cut
    }

    /// `YYYY-MM-DD` and `HHMMSS` for a time, in the Mac's time zone.
    static func stamp(_ date: Date, timeZone: TimeZone = .current) -> (day: String, time: String) {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = timeZone
        f.dateFormat = "yyyy-MM-dd"
        let day = f.string(from: date)
        f.dateFormat = "HHmmss"
        return (day, f.string(from: date))
    }

    /// ISO 8601 with a numeric offset, e.g. `2026-10-17T14:03:22+01:00`.
    static func timestamp(_ date: Date, timeZone: TimeZone = .current) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = timeZone
        f.dateFormat = "yyyy-MM-dd'T'HH:mm:ssxxx"
        return f.string(from: date)
    }

    /// Writes the JPEG (as the phone sent it) and its sidecar; returns the JPEG's URL.
    static func save(jpeg: Data, sidecar: [String: Any], note: String?, in folder: URL,
                     at date: Date = Date()) throws -> URL {
        let (day, time) = stamp(date)
        let dir = folder.appending(path: day, directoryHint: .isDirectory)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let s = slug(note)
        let base = s.isEmpty ? time : "\(time)-\(s)"
        for n in 1... {
            let stem = n == 1 ? base : "\(base)-\(n)"
            let jpg = dir.appending(path: "\(stem).jpg"), json = dir.appending(path: "\(stem).json")
            if FileManager.default.fileExists(atPath: json.path) { continue }
            do {
                try jpeg.write(to: jpg, options: .withoutOverwriting)
            } catch CocoaError.fileWriteFileExists {
                continue
            }
            var meta = sidecar
            meta["name"] = "\(stem).jpg"
            meta["bytes"] = jpeg.count
            let data = try JSONSerialization.data(withJSONObject: meta, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
            try data.write(to: json)
            return jpg
        }
        fatalError("unreachable")
    }
}
