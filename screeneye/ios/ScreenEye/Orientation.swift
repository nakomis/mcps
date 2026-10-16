import CoreImage
import CoreMotion
import Foundation

/// Which way up the phone is, from gravity, and the rotation that makes a frame upright.
///
/// Frames come from the sensor in its native landscape: upright when the phone is held in
/// landscape with its top to the left (home side to the right), `UIDeviceOrientation.landscapeLeft`.
/// `rotation` is how far to turn a raw frame **clockwise**, in degrees, to make it upright.
enum DeviceOrientation: String, Sendable, CaseIterable {
    case unknown, portrait, portraitUpsideDown, landscapeLeft, landscapeRight, faceUp, faceDown

    /// Clockwise degrees for an upright frame. Only meaningful for the four upright
    /// orientations; flat and unknown ones have none of their own (see `Orientation.update`).
    var ownRotation: Int? {
        switch self {
        case .landscapeLeft: 0
        case .portrait: 90
        case .landscapeRight: 180
        case .portraitUpsideDown: 270
        case .unknown, .faceUp, .faceDown: nil
        }
    }

    var isFlat: Bool { self == .faceUp || self == .faceDown }
}

/// The current orientation and the rotation in force.
struct OrientationState: Equatable, Sendable {
    var orientation: DeviceOrientation = .unknown
    /// Clockwise degrees: 0, 90, 180 or 270.
    var rotation: Int = 0
}

enum OrientationMath {
    /// Below this much gravity in the screen's plane (in g), the phone counts as flat:
    /// within about 30° of lying face up or face down.
    static let flatThreshold = 0.5
    /// How far past a 45° diagonal the phone must turn before an upright orientation changes,
    /// so it doesn't flicker between two when held near the diagonal.
    static let hysteresisDegrees = 10.0

    /// The next state for a gravity vector in CoreMotion's device frame (x to the right of the
    /// screen, y to its top, z out of the screen; in g), given the previous state.
    ///
    /// Flat (face up or face down): the orientation says so, but the rotation **keeps its last
    /// value**. Laid flat, gravity says nothing about which way is up in the picture, and a
    /// phone tipped down onto a desk or bed was usually upright a moment before, so the last
    /// rotation is the best guess; 0 (raw) if there has never been one.
    static func update(_ previous: OrientationState, gx: Double, gy: Double, gz: Double) -> OrientationState {
        guard gx.isFinite, gy.isFinite, gz.isFinite else { return previous }
        let inPlane = (gx * gx + gy * gy).squareRoot()
        if inPlane < flatThreshold {
            if inPlane == 0 && gz == 0 { return previous }  // no reading
            return OrientationState(orientation: gz <= 0 ? .faceUp : .faceDown, rotation: previous.rotation)
        }
        // The angle of "down" on the screen, turned so that 0° is landscapeLeft and it grows
        // clockwise in the picture: landscapeLeft 0, portrait 90, landscapeRight 180, upside-down 270.
        let angle = normalise(atan2(gy, gx) * 180 / .pi + 180)
        let nearest = Int((angle / 90).rounded()) % 4 * 90
        // The rotation in force: after lying flat, the one kept from before.
        let inForce = previous.orientation.ownRotation ?? (previous.orientation.isFlat ? previous.rotation : nil)
        if let current = inForce, current != nearest,
           distance(angle, Double(current)) < 45 + hysteresisDegrees {
            // Still within the dead band around the current orientation.
            return OrientationState(orientation: orientation(forRotation: current), rotation: current)
        }
        return OrientationState(orientation: orientation(forRotation: nearest), rotation: nearest)
    }

    static func orientation(forRotation r: Int) -> DeviceOrientation {
        switch r {
        case 90: .portrait
        case 180: .landscapeRight
        case 270: .portraitUpsideDown
        default: .landscapeLeft
        }
    }

    /// The `CGImagePropertyOrientation` that displays a raw frame turned `rotation` degrees clockwise.
    static func imageOrientation(rotation: Int) -> CGImagePropertyOrientation {
        switch rotation {
        case 90: .right
        case 180: .down
        case 270: .left
        default: .up
        }
    }

    private static func normalise(_ deg: Double) -> Double {
        let d = deg.truncatingRemainder(dividingBy: 360)
        return d < 0 ? d + 360 : d
    }

    private static func distance(_ a: Double, _ b: Double) -> Double {
        let d = abs(normalise(a) - normalise(b))
        return min(d, 360 - d)
    }
}

/// Follows gravity with CoreMotion, which works whatever the interface orientation (or its
/// lock) is, and when the phone lies flat. Read `state` from any thread.
final class OrientationTracker: @unchecked Sendable {
    private let motion = CMMotionManager()
    private let queue: OperationQueue = {
        let q = OperationQueue()
        q.name = "orientation"
        q.maxConcurrentOperationCount = 1
        return q
    }()
    private let lock = NSLock()
    private var current = OrientationState()

    var state: OrientationState {
        lock.lock(); defer { lock.unlock() }
        return current
    }

    func start() {
        guard motion.isDeviceMotionAvailable, !motion.isDeviceMotionActive else { return }
        motion.deviceMotionUpdateInterval = 0.2
        motion.startDeviceMotionUpdates(to: queue) { [weak self] m, _ in
            guard let self, let g = m?.gravity else { return }
            self.lock.lock()
            self.current = OrientationMath.update(self.current, gx: g.x, gy: g.y, gz: g.z)
            self.lock.unlock()
        }
    }
}
