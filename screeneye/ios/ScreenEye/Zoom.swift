import Foundation

/// Zoom arithmetic shared by the on-screen controls and `GET /set?zoom=`.
/// Zooms here are multiples of the 1x (wide) lens, as `/status` reports them.
enum ZoomMath {
    /// Digital steps offered when the device has only one lens (most iPads), so there's
    /// still something to tap.
    static let digitalPresets: [Double] = [2, 5]

    static func clamp<T: Comparable>(_ value: T, _ lower: T, _ upper: T) -> T {
        max(lower, min(value, upper))
    }

    /// The quick steps offered on screen: the device's lens steps within its range (with a
    /// little slack for rounding), plus a couple of digital ones if it has only one lens.
    static func steps(lenses: [Double], min lower: Double, max upper: Double) -> [Double] {
        let inRange = { (z: Double) in z >= lower - 0.01 && z <= upper + 0.01 }
        var steps = Array(Set(lenses.filter(inRange))).sorted()
        if steps.count < 2 {
            steps += digitalPresets.filter { z in inRange(z) && !steps.contains { abs($0 - z) < 0.05 } }
        }
        return steps.sorted()
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
