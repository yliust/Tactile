import AppKit
import CoreGraphics
import Foundation

func main() -> Int32 {
    let args = CommandLine.arguments
    guard args.count >= 3, let windowID = CGWindowID(args[1]) else {
        fputs("usage: screenshot-helper <windowID> <outputPath> [--click <x>,<y> --bounds <x>,<y>,<w>,<h>]\n", stderr)
        return 1
    }

    let outputPath = args[2]
    var clickPoint: CGPoint?
    var windowRect: CGRect?
    var i = 3
    while i < args.count {
        if args[i] == "--click", i + 1 < args.count {
            let parts = args[i + 1].split(separator: ",").compactMap { Double($0) }
            if parts.count == 2 {
                clickPoint = CGPoint(x: parts[0], y: parts[1])
            }
            i += 2
        } else if args[i] == "--bounds", i + 1 < args.count {
            let parts = args[i + 1].split(separator: ",").compactMap { Double($0) }
            if parts.count == 4 {
                windowRect = CGRect(x: parts[0], y: parts[1], width: parts[2], height: parts[3])
            }
            i += 2
        } else {
            i += 1
        }
    }

    guard let image = CGWindowListCreateImage(.null, .optionIncludingWindow, windowID, [.boundsIgnoreFraming, .bestResolution]) else {
        fputs("error: CGWindowListCreateImage failed for window \(windowID)\n", stderr)
        return 1
    }

    var finalImage = image
    if let clickPoint, let windowRect {
        let imageWidth = CGFloat(image.width)
        let imageHeight = CGFloat(image.height)
        let scaleX = imageWidth / max(windowRect.width, 1)
        let scaleY = imageHeight / max(windowRect.height, 1)
        let localX = (clickPoint.x - windowRect.minX) * scaleX
        let localY = (clickPoint.y - windowRect.minY) * scaleY

        let colorSpace = CGColorSpaceCreateDeviceRGB()
        if let ctx = CGContext(
            data: nil,
            width: image.width,
            height: image.height,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) {
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: imageWidth, height: imageHeight))
            let drawX = localX
            let drawY = imageHeight - localY
            let scale = max(scaleX, scaleY)

            ctx.setStrokeColor(CGColor(red: 1, green: 0, blue: 0, alpha: 1))
            ctx.setLineWidth(2.0 * scale)
            let armLength: CGFloat = 15 * scale
            ctx.move(to: CGPoint(x: drawX - armLength, y: drawY))
            ctx.addLine(to: CGPoint(x: drawX + armLength, y: drawY))
            ctx.move(to: CGPoint(x: drawX, y: drawY - armLength))
            ctx.addLine(to: CGPoint(x: drawX, y: drawY + armLength))
            ctx.strokePath()

            ctx.setLineWidth(1.5 * scale)
            let radius: CGFloat = 10 * scale
            ctx.addEllipse(in: CGRect(x: drawX - radius, y: drawY - radius, width: radius * 2, height: radius * 2))
            ctx.strokePath()

            if let annotatedImage = ctx.makeImage() {
                finalImage = annotatedImage
            }
        }
    }

    let bitmapRep = NSBitmapImageRep(cgImage: finalImage)
    guard let pngData = bitmapRep.representation(using: .png, properties: [:]) else {
        fputs("error: failed to create PNG data\n", stderr)
        return 1
    }

    do {
        try FileManager.default.createDirectory(
            at: URL(fileURLWithPath: (outputPath as NSString).deletingLastPathComponent),
            withIntermediateDirectories: true
        )
        try pngData.write(to: URL(fileURLWithPath: outputPath))
        print(outputPath)
        return 0
    } catch {
        fputs("error: failed to write screenshot: \(error.localizedDescription)\n", stderr)
        return 1
    }
}

exit(main())
