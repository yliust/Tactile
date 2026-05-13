import XCTest
@testable import MacosUseSDK

final class VirtualCursorTests: XCTestCase {
    func testVirtualCursorStateDecoding() throws {
        let json = """
        {
          "visible": true,
          "point": {"x": 100.0, "y": 200.0},
          "event": "click",
          "label": null,
          "updatedAt": 1710000000.0
        }
        """
        let state = try JSONDecoder().decode(VirtualCursorState.self, from: Data(json.utf8))
        XCTAssertTrue(state.visible)
        XCTAssertEqual(state.point, VirtualCursorPoint(x: 100, y: 200))
        XCTAssertEqual(state.event, "click")
        XCTAssertNil(state.label)
    }

    @MainActor
    func testOverlayWindowIgnoresMouseEvents() {
        let window = makeVirtualCursorWindowForTesting()
        XCTAssertTrue(window.ignoresMouseEvents)
        XCTAssertFalse(window.isOpaque)
        XCTAssertEqual(window.frame.width, 300)
        XCTAssertEqual(window.frame.height, 200)
    }

    func testVirtualCursorLocalPointMapsTopLeftQuartzToBottomLeftView() {
        let local = virtualCursorLocalPoint(
            forTopLeftScreenPoint: CGPoint(x: 435, y: -486),
            displayBounds: CGRect(x: -78, y: -1080, width: 1920, height: 1080)
        )
        XCTAssertEqual(local.x, 513)
        XCTAssertEqual(local.y, 486)
    }

    func testDisplaySpecificCoordinateConversionHandlesNegativeQuartzY() {
        let frame = appKitFrameFromTopLeftScreenRect(
            CGRect(x: 82, y: -498, width: 132, height: 92),
            displayTopLeftBounds: CGRect(x: -78, y: -1080, width: 1920, height: 1080),
            appKitScreenFrame: NSRect(x: -78, y: 0, width: 1920, height: 1080)
        )
        XCTAssertEqual(frame.minX, 82)
        XCTAssertEqual(frame.minY, 406)
    }

    func testBackgroundCursorRendererDrawsArrowPixels() throws {
        guard let context = CGContext(
            data: nil,
            width: 80,
            height: 80,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            XCTFail("failed to make context")
            return
        }
        let accent = CursorAccentPalette.derive(from: NSColor.presenceCursorColor(hex: "#0095A1"))
        let snapshot = CursorSnapshot(
            cursorID: "test",
            attachedWindowNumber: 0,
            attachedWindowLevelRawValue: 0,
            position: CGPoint(x: 40, y: 40),
            angle: CursorMotionConstants.arrowHomeAngle,
            scale: 1.08,
            alpha: 1,
            glyph: .arrow,
            previousGlyph: nil,
            morphProgress: 1,
            isPressed: false,
            accent: accent,
            baseColor: .white,
            pivotLocal: CursorPivotKind.tip.pathPoint,
            labelText: "",
            labelAlpha: 0,
            labelScale: 1,
            trailHistories: [],
            trailVisible: true,
            caretPhase: 0,
            anticipationTilt: 0,
            effects: []
        )
        CursorRenderer.draw(snapshot, in: context)
        let image = try XCTUnwrap(context.makeImage())
        XCTAssertGreaterThan(nonTransparentPixelCount(in: image), 20)
    }

    private func nonTransparentPixelCount(in image: CGImage) -> Int {
        let width = image.width
        let height = image.height
        let bytesPerRow = width * 4
        var bytes = [UInt8](repeating: 0, count: bytesPerRow * height)
        let wrote = bytes.withUnsafeMutableBytes { rawBuffer -> Bool in
            guard let baseAddress = rawBuffer.baseAddress,
                  let context = CGContext(
                      data: baseAddress,
                      width: width,
                      height: height,
                      bitsPerComponent: 8,
                      bytesPerRow: bytesPerRow,
                      space: CGColorSpaceCreateDeviceRGB(),
                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
                  ) else {
                return false
            }
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard wrote else { return 0 }
        var count = 0
        for offset in stride(from: 0, to: bytes.count, by: 4) where bytes[offset + 3] > 0 {
            count += 1
        }
        return count
    }
}
