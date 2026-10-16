import Foundation

/// Zoom arithmetic shared by the on-screen controls and `GET /set?zoom=`.
/// Zooms here are multiples of the 1x (wide) lens, as `/status` reports them.
enum ZoomMath {
    /// The quick steps offered on screen, where the device can reach them.
    static let presets: [Double] = [0.5, 1, 2, 5]

    static func clamp<T: Comparable>(_ value: T, _ lower: T, _ upper: T) -> T {
        max(lower, min(value, upper))
    }

    /// The presets within the device's range (with a little slack for rounding).
    static func steps(min lower: Double, max upper: Double) -> [Double] {
        presets.filter { $0 >= lower - 0.01 && $0 <= upper + 0.01 }
    }

    /// Slider position (0…1) for a zoom. The scale is logarithmic, so 1×→2× is as
    /// much travel as 10×→20×.
    static func sliderPosition(zoom: Double, min lower: Double, max upper: Double) -> Double {
        guard lower > 0, upper > lower else { return 0 }
        return clamp(log(clamp(zoom, lower, upper) / lower) / log(upper / lower), 0, 1)
    }

    /// The zoom at a slider position (0…1).
    static func zoom(sliderPosition t: Double, min lower: Double, max upper: Double) -> Double {
        guard lower > 0, upper > lower else { return lower }
        return lower * pow(upper / lower, clamp(t, 0, 1))
    }

    /// "0.5×", "2×", "2.3×", "12×".
    static func label(_ zoom: Double) -> String {
        if zoom >= 10 { return "\(Int(zoom.rounded()))×" }
        let tenths = (zoom * 10).rounded() / 10
        return tenths == tenths.rounded() ? "\(Int(tenths))×" : String(format: "%.1f×", tenths)
    }
}
