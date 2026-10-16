import CoreGraphics
import Foundation
import UIKit

/// Points and rectangles as fractions of a frame, (0,0) top-left, (1,1) bottom-right, and
/// turning them between the raw sensor frame and a frame turned `rotation` degrees clockwise
/// (the upright frame of EYE-3, or the on-screen preview).
enum FrameMath {
    /// A raw point, in the frame turned `rotation` degrees clockwise.
    static func turned(_ p: CGPoint, rotation: Int) -> CGPoint {
        switch rotation {
        case 90: CGPoint(x: 1 - p.y, y: p.x)
        case 180: CGPoint(x: 1 - p.x, y: 1 - p.y)
        case 270: CGPoint(x: p.y, y: 1 - p.x)
        default: p
        }
    }

    /// A point in the frame turned `rotation` degrees clockwise, back in the raw frame.
    static func raw(_ p: CGPoint, rotation: Int) -> CGPoint {
        turned(p, rotation: (360 - rotation) % 360)
    }

    static func turned(_ r: CGRect, rotation: Int) -> CGRect {
        let a = turned(CGPoint(x: r.minX, y: r.minY), rotation: rotation)
        let b = turned(CGPoint(x: r.maxX, y: r.maxY), rotation: rotation)
        return CGRect(x: min(a.x, b.x), y: min(a.y, b.y), width: abs(a.x - b.x), height: abs(a.y - b.y))
    }

    /// A movement (not a position) in the turned frame, back in the raw frame.
    static func rawVector(_ v: CGVector, rotation: Int) -> CGVector {
        let a = raw(CGPoint(x: v.dx, y: v.dy), rotation: rotation)
        let o = raw(.zero, rotation: rotation)
        return CGVector(dx: a.x - o.x, dy: a.y - o.y)
    }
}

/// What the picture shows: a zoom made of the lens's (optical) zoom and a digital crop of
/// its frame, centred where the user panned to. All in the raw sensor frame.
struct Viewport: Equatable, Sendable {
    /// The zoom asked for, in multiples of the 1x lens.
    var zoom: Double = 1
    /// The device's own zoom: the lens step at or below `zoom`.
    var optical: Double = 1
    /// The centre of the visible region, as fractions of the lens's frame.
    var center = CGPoint(x: 0.5, y: 0.5)

    /// How much of the lens's frame is cropped away: 1 shows all of it.
    var digital: Double { optical > 0 ? max(zoom / optical, 1) : 1 }

    /// The visible region, as fractions of the lens's frame.
    var rect: CGRect {
        let s = 1 / digital
        return CGRect(x: center.x - s / 2, y: center.y - s / 2, width: s, height: s)
    }

    /// A point in the picture (fractions of the visible region), in the lens's frame.
    func framePoint(_ p: CGPoint) -> CGPoint {
        let r = rect
        return CGPoint(x: r.minX + p.x * r.width, y: r.minY + p.y * r.height)
    }
}

enum ViewportMath {
    /// The lens step to use for a zoom: the largest at or below it (with rounding slack), or
    /// the smallest when the zoom is below them all.
    static func optical(for zoom: Double, lenses: [Double]) -> Double {
        let sorted = lenses.filter { $0 > 0 }.sorted()
        guard let first = sorted.first else { return max(zoom, 0) }
        return sorted.last { $0 <= zoom + 0.001 } ?? first
    }

    /// The centre moved as little as needed to keep the visible region inside the frame.
    static func clampCenter(_ c: CGPoint, digital: Double) -> CGPoint {
        let half = 0.5 / max(digital, 1)
        func clamp(_ v: Double) -> Double { v.isFinite ? ZoomMath.clamp(v, half, 1 - half) : 0.5 }
        return CGPoint(x: clamp(c.x), y: clamp(c.y))
    }

    /// The viewport for a zoom and centre, within the device's range.
    static func make(zoom: Double, center: CGPoint, lenses: [Double], min lower: Double, max upper: Double) -> Viewport {
        let z = ZoomMath.clamp(zoom.isFinite ? zoom : 1, lower, Swift.max(lower, upper))
        var v = Viewport(zoom: z, optical: optical(for: z, lenses: lenses), center: center)
        v.center = clampCenter(center, digital: v.digital)
        return v
    }

    /// The size of a frame of aspect `aspect` (width / height) fitted inside `bounds`.
    static func fitted(aspect: Double, in bounds: CGSize) -> CGSize {
        guard aspect > 0, bounds.width > 0, bounds.height > 0 else { return bounds }
        return bounds.width / bounds.height > aspect
            ? CGSize(width: bounds.height * aspect, height: bounds.height)
            : CGSize(width: bounds.width, height: bounds.width / aspect)
    }

    /// The raw frame's aspect as shown turned `angle` degrees clockwise.
    static func shownAspect(raw aspect: Double, angle: Int) -> Double {
        angle == 90 || angle == 270 ? 1 / aspect : aspect
    }

    /// The transform, about the preview's centre, that makes the visible region fill the
    /// preview. `angle` is the preview's clockwise turn of the raw frame; `shown` the size of
    /// the frame on screen (fitted in the preview).
    static func previewTransform(_ v: Viewport, angle: Int, shown: CGSize) -> CGAffineTransform {
        let r = FrameMath.turned(v.rect, rotation: angle)
        let d = v.digital
        return CGAffineTransform(a: d, b: 0, c: 0, d: d,
                                 tx: -(r.midX - 0.5) * shown.width * d,
                                 ty: -(r.midY - 0.5) * shown.height * d)
    }

    /// The raw centre after dragging the preview by `translation` points: the picture follows
    /// the finger, so the centre moves the other way.
    static func dragged(from center: CGPoint, translation: CGSize, angle: Int, shown: CGSize, digital: Double) -> CGPoint {
        guard shown.width > 0, shown.height > 0 else { return center }
        let onScreen = CGVector(dx: -translation.width / (shown.width * digital),
                                dy: -translation.height / (shown.height * digital))
        let v = FrameMath.rawVector(onScreen, rotation: angle)
        return CGPoint(x: center.x + v.dx, y: center.y + v.dy)
    }

    /// The clockwise turn the preview gives the raw frame for an interface orientation.
    static func previewAngle(_ o: UIInterfaceOrientation?) -> Int {
        switch o {
        case .landscapeRight: 0  // home side to the right: the sensor's own way up
        case .landscapeLeft: 180
        case .portraitUpsideDown: 270
        default: 90
        }
    }
}
