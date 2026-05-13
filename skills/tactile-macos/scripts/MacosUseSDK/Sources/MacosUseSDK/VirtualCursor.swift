import AppKit
import Foundation

public struct VirtualCursorPoint: Codable, Equatable, Sendable {
    public var x: Double
    public var y: Double

    public init(x: Double, y: Double) {
        self.x = x
        self.y = y
    }
}

public struct VirtualCursorState: Codable, Equatable, Sendable {
    public var visible: Bool
    public var point: VirtualCursorPoint
    public var event: String
    public var label: String?
    public var updatedAt: Double

    public init(visible: Bool, point: VirtualCursorPoint, event: String, label: String?, updatedAt: Double) {
        self.visible = visible
        self.point = point
        self.event = event
        self.label = label
        self.updatedAt = updatedAt
    }
}

internal func virtualCursorLocalPoint(forTopLeftScreenPoint point: CGPoint, displayBounds: CGRect) -> CGPoint {
    CGPoint(
        x: point.x - displayBounds.minX,
        y: displayBounds.maxY - point.y
    )
}

internal final class VirtualCursorView: NSView {
    var displayBounds: CGRect {
        didSet {
            guard oldValue != displayBounds else { return }
            resetMotion()
        }
    }

    private var state: VirtualCursorState?
    private var currentPosition: CGPoint?
    private var targetPosition: CGPoint?
    private var motionPlan: CursorMotionPlan?
    private var history: [CGPoint] = []
    private var lastFrameTime = CACurrentMediaTime()
    private var angle: CGFloat = CursorMotionConstants.arrowHomeAngle
    private var effects: [CursorVisualEffect] = []
    private var startedAt = CACurrentMediaTime()
    private var pressedUntil: TimeInterval = 0
    private var lastEffectUpdatedAt: Double?
    private let accent = CursorAccentPalette.derive(from: NSColor.presenceCursorColor(hex: CursorProfile.codex.colorHex))

    init(frame frameRect: NSRect, displayBounds: CGRect) {
        self.displayBounds = displayBounds
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.clear.cgColor
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override var isFlipped: Bool { false }
    override var isOpaque: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard let state,
              state.visible,
              currentPosition != nil,
              let context = NSGraphicsContext.current?.cgContext else {
            return
        }

        context.saveGState()
        context.clear(bounds)
        CursorRenderer.draw(snapshot(for: state, at: CACurrentMediaTime()), in: context)
        context.restoreGState()
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    func apply(_ state: VirtualCursorState) {
        self.state = state
        guard state.visible else {
            resetMotion()
            needsDisplay = true
            return
        }

        let point = virtualCursorLocalPoint(
            forTopLeftScreenPoint: CGPoint(x: state.point.x, y: state.point.y),
            displayBounds: displayBounds
        )

        if let targetPosition, targetPosition.distance(to: point) < 0.5 {
            handleEventEffect(for: state, at: point)
            return
        }

        targetPosition = point
        startMotion(to: point, event: state.event)
        handleEventEffect(for: state, at: point)
        needsDisplay = true
    }

    func tick() {
        guard let state, state.visible else { return }

        let now = CACurrentMediaTime()
        let dt = CGFloat(min(max(now - lastFrameTime, 1.0 / 240.0), 1.0 / 24.0))
        lastFrameTime = now

        if let plan = motionPlan {
            currentPosition = plan.samplePoint(at: now)
            angle = CursorMotionConstants.arrowHomeAngle

            if plan.isFinished(at: now) {
                currentPosition = plan.end
                motionPlan = nil
            }
        }

        if let currentPosition {
            appendHistory(currentPosition)
        }
        effects = effects
            .map { $0.advanced(by: TimeInterval(dt)) }
            .filter { $0.finished == false }

        needsDisplay = true
    }

    private func startMotion(to point: CGPoint, event: String) {
        let now = CACurrentMediaTime()
        let start: CGPoint
        let entrance: Bool

        if let currentPosition {
            start = currentPosition
            entrance = false
        } else {
            start = CursorMotionPlanner.edgeEntrancePoint(for: bounds.insetBy(dx: 80, dy: 80))
            currentPosition = start
            history = [start]
            entrance = true
            startedAt = now
        }

        let distance = start.distance(to: point)
        let forcedDuration: TimeInterval?
        if event == "move_test" {
            forcedDuration = max(0.72, min(1.45, MotionPacing.transitDuration(for: distance, entrance: entrance)))
        } else {
            forcedDuration = nil
        }

        motionPlan = CursorMotionPlanner.plan(
            from: start,
            to: point,
            now: now,
            entrance: entrance,
            forcedDuration: forcedDuration
        )
    }

    private func appendHistory(_ point: CGPoint) {
        if let last = history.last, last.distance(to: point) < 0.45 {
            return
        }
        history.append(point)
        if history.count > CursorMotionConstants.trailHistoryLength {
            history.removeFirst(history.count - CursorMotionConstants.trailHistoryLength)
        }
    }

    private func resetMotion() {
        state = nil
        currentPosition = nil
        targetPosition = nil
        motionPlan = nil
        history.removeAll()
        effects.removeAll()
        angle = CursorMotionConstants.arrowHomeAngle
        pressedUntil = 0
        lastEffectUpdatedAt = nil
        lastFrameTime = CACurrentMediaTime()
    }

    private func handleEventEffect(for state: VirtualCursorState, at point: CGPoint) {
        guard lastEffectUpdatedAt != state.updatedAt else { return }
        lastEffectUpdatedAt = state.updatedAt
        guard state.event == "click" else { return }

        let now = CACurrentMediaTime()
        pressedUntil = now + CursorActionTimings.defaults.clickPressHoldMilliseconds / 1000
        effects.append(
            .ripple(
                origin: point,
                color: accent.fill,
                maxRadius: 30,
                thickness: 2.2,
                lifetime: 0.62,
                age: 0
            )
        )
        effects.append(
            .sparkRing(
                origin: point,
                color: accent.trail,
                count: 10,
                lifetime: 0.68,
                age: 0,
                rngSeed: UInt64.random(in: 0..<9999)
            )
        )
    }

    private func snapshot(for state: VirtualCursorState, at now: TimeInterval) -> CursorSnapshot {
        let point = currentPosition ?? targetPosition ?? .zero
        let breath = CGFloat((sin((now - startedAt) * 3.8) + 1) / 2)

        return CursorSnapshot(
            cursorID: "tactile-macos",
            attachedWindowNumber: window?.windowNumber ?? 0,
            attachedWindowLevelRawValue: window?.level.rawValue ?? NSWindow.Level.floating.rawValue,
            position: point,
            angle: angle,
            scale: 1.08 + breath * 0.04,
            alpha: 1,
            glyph: .arrow,
            previousGlyph: nil,
            morphProgress: 1,
            isPressed: now < pressedUntil,
            accent: accent,
            baseColor: .white,
            pivotLocal: CursorPivotKind.tip.pathPoint,
            labelText: "",
            labelAlpha: 0,
            labelScale: 1,
            trailHistories: history.count > 1 ? [history] : [],
            trailVisible: CursorMotionConstants.trailVisible,
            caretPhase: 0,
            anticipationTilt: 0,
            effects: effects
        )
    }
}

@MainActor
public func makeVirtualCursorWindowForTesting() -> NSWindow {
    makeVirtualCursorWindow(
        frame: NSRect(x: 0, y: 0, width: 300, height: 200),
        displayBounds: CGRect(x: 0, y: 0, width: 300, height: 200)
    )
}

@MainActor
internal func makeVirtualCursorWindow(frame: NSRect, displayBounds: CGRect) -> NSWindow {
    let window = NSWindow(contentRect: frame, styleMask: [.borderless], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.isOpaque = false
    window.backgroundColor = .clear
    window.hasShadow = false
    window.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.screenSaverWindow)))
    window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle, .transient]
    window.isMovableByWindowBackground = false
    window.ignoresMouseEvents = true
    window.contentView = VirtualCursorView(frame: NSRect(origin: .zero, size: frame.size), displayBounds: displayBounds)
    return window
}

@MainActor
public final class VirtualCursorController: NSObject {
    private let stateURL: URL
    private var window: NSWindow?
    private var timer: Timer?
    private var lastState: VirtualCursorState?

    public init(statePath: String) {
        self.stateURL = URL(fileURLWithPath: statePath)
        super.init()
    }

    public func start() {
        timer?.invalidate()
        let newTimer = Timer(timeInterval: 1.0 / 60.0, target: self, selector: #selector(timerFired(_:)), userInfo: nil, repeats: true)
        timer = newTimer
        RunLoop.main.add(newTimer, forMode: .common)
        refresh()
    }

    public func stop() {
        timer?.invalidate()
        timer = nil
        window?.orderOut(nil)
        window = nil
    }

    @objc private func timerFired(_ timer: Timer) {
        refresh()
        if let view = window?.contentView as? VirtualCursorView {
            view.tick()
        }
    }

    private func refresh() {
        guard let data = try? Data(contentsOf: stateURL),
              let state = try? JSONDecoder().decode(VirtualCursorState.self, from: data) else {
            return
        }
        guard state != lastState else { return }
        lastState = state
        apply(state)
    }

    private func apply(_ state: VirtualCursorState) {
        guard state.visible else {
            if let view = window?.contentView as? VirtualCursorView {
                view.apply(state)
            }
            window?.orderOut(nil)
            return
        }

        let point = CGPoint(x: state.point.x, y: state.point.y)
        let bridge = displayBridgeFrame(containing: point)
        let frame = bridge?.screenFrame ?? NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1, height: 1)
        let displayBounds = bridge?.displayBounds ?? CGDisplayBounds(CGMainDisplayID())
        let targetWindow = window ?? makeVirtualCursorWindow(frame: frame, displayBounds: displayBounds)
        window = targetWindow
        if targetWindow.frame != frame {
            targetWindow.setFrame(frame, display: true)
        }
        if let view = targetWindow.contentView as? VirtualCursorView {
            view.displayBounds = displayBounds
            view.frame = NSRect(origin: .zero, size: frame.size)
            view.apply(state)
        }
        targetWindow.orderFrontRegardless()
    }
}
