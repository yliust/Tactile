import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import MCP
import MacosUseSDK
import Vision

let outputRoot = "/tmp/tactile-macos-mcp"
let secondaryActionEnum = [
    "Press", "Raise", "ShowMenu", "Confirm", "Cancel", "Increment", "Decrement",
    "Focus", "Select", "Deselect", "ScrollUp", "ScrollDown", "ScrollLeft", "ScrollRight",
]
let coordinateSpaceEnum = ["screenshot", "screen"]
let observationModeEnum = ["ax", "ax_ocr", "ax_ocr_visual"]
let defaultOCRLanguages = "zh-Hans,en-US"
let ocrRecognitionLevelEnum = ["accurate", "fast"]
let summaryModeEnum = ["compact", "full", "metadata"]
let defaultSummaryElementLimit = 80
let summaryTextMaxLength = 500
let elementFilterDescription = """
Case-insensitive regular expression filter for narrowing the get_app_state summary. It matches each element's index, source, role, visible text, AX path, state flags, and secondary action names. Use plain text for one term, for example "张仲岳"; use regex alternation with | for multiple terms, for example "search|搜索|输入|联系人|张仲岳". Escape regex metacharacters when you need them literally. If the pattern is not valid regex, matching falls back to a case-insensitive literal contains check. element_filter only filters the tool output; it does not search, type, focus, or change the app. Full state files are not filtered. Increase element_limit if many elements match.
"""

enum ObservationMode: String, Codable, Sendable {
    case ax
    case axOCR = "ax_ocr"
    case axOCRVisual = "ax_ocr_visual"

    var includesOCR: Bool {
        self == .axOCR || self == .axOCRVisual
    }

    var includesVisual: Bool {
        self == .axOCRVisual
    }
}

enum SummaryMode: String, Sendable {
    case compact
    case full
    case metadata
}

struct SummaryOptions: Sendable {
    var mode: SummaryMode = .compact
    var elementLimit: Int = defaultSummaryElementLimit
    var elementFilter: String? = nil
}

struct Frame: Codable, Sendable {
    var x: Double
    var y: Double
    var width: Double
    var height: Double

    var rect: CGRect { CGRect(x: x, y: y, width: width, height: height) }

    init(_ rect: CGRect) {
        self.x = rect.origin.x
        self.y = rect.origin.y
        self.width = rect.width
        self.height = rect.height
    }

    init(x: Double, y: Double, width: Double, height: Double) {
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }
}

struct ScreenshotInfo: Codable, Sendable {
    var path: String
    var windowFrame: Frame
    var pixelWidth: Int
    var pixelHeight: Int

    var scaleX: Double { Double(pixelWidth) / max(windowFrame.width, 1) }
    var scaleY: Double { Double(pixelHeight) / max(windowFrame.height, 1) }
}

struct PointInfo: Codable, Sendable {
    var x: Double
    var y: Double
}

struct OCRLine: Codable, Sendable {
    var text: String
    var confidence: Double
    var frame: Frame
    var imageFrame: Frame
    var screenFrame: Frame
    var screenCenter: PointInfo
}

struct OCRSource: Codable, Sendable {
    var kind: String
    var region: Frame
    var screenshot: String
}

struct OCRCoordinateSpace: Codable, Sendable {
    var frame: String
    var imageFrame: String
    var screenFrame: String
}

struct OCRPayload: Codable, Sendable {
    var image: String
    var imageWidth: Int
    var imageHeight: Int
    var languages: [String]
    var recognitionLevel: String
    var lines: [OCRLine]
    var source: OCRSource
    var coordinateSpace: OCRCoordinateSpace
}

struct VisualCoordinateSpace: Codable, Sendable {
    var frame: String
    var screenshotRegion: Frame
    var screenshotPixels: PointInfo
    var rule: String
}

struct VisualObservation: Codable, Sendable {
    var enabled: Bool
    var imageAttachedToToolResult: Bool
    var screenshotPath: String?
    var coordinateSpace: VisualCoordinateSpace?
    var error: String?
}

struct IndexedElement: Codable, Sendable {
    var index: String
    var source: String = "ax"
    var role: String
    var text: String?
    var screenFrame: Frame?
    var screenshotFrame: Frame? = nil
    var screenCenter: PointInfo?
    var screenshotCenter: PointInfo? = nil
    var confidence: Double? = nil
    var axPath: String?
    var settable: Bool
    var focused: Bool?
    var selected: Bool?
    var secondaryActions: [String]
}

struct AppState: Codable, Sendable {
    var requestedApp: String
    var appName: String
    var bundleIdentifier: String?
    var pid: Int32
    var windowTitle: String?
    var screenshot: ScreenshotInfo?
    var observationMode: ObservationMode
    var elements: [IndexedElement]
    var focusedElementIndex: String?
    var ocrPayload: OCRPayload?
    var ocrError: String?
    var visualObservation: VisualObservation?
    var traversal: ResponseData
    var statePath: String?
    var textPath: String?
    var createdAt: Double
}

struct AppRecord: Codable, Sendable {
    var name: String
    var bundleIdentifier: String?
    var path: String?
    var pid: Int32?
    var running: Bool
    var frontmost: Bool
}

enum TactileMCPError: Error, LocalizedError {
    case invalidArgument(String)
    case appStateUnavailable(String)
    case elementNotFound(String)
    case unsupportedAction(String)
    case actionFailed(String)

    var errorDescription: String? {
        switch self {
        case .invalidArgument(let message),
             .appStateUnavailable(let message),
             .elementNotFound(let message),
             .unsupportedAction(let message),
             .actionFailed(let message):
            return message
        }
    }
}

struct WindowCaptureInfo: Sendable {
    var windowID: CGWindowID
    var frame: CGRect
    var layer: Int
    var alpha: Double

    var area: CGFloat {
        max(0, frame.width) * max(0, frame.height)
    }
}

func jsonString<T: Encodable>(_ value: T, pretty: Bool = true) throws -> String {
    let encoder = JSONEncoder()
    if pretty {
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
    } else {
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
    }
    let data = try encoder.encode(value)
    return String(data: data, encoding: .utf8) ?? "{}"
}

func nowMillis() -> Int64 {
    Int64(Date().timeIntervalSince1970 * 1000)
}

func ensureOutputDir() throws {
    try FileManager.default.createDirectory(atPath: outputRoot, withIntermediateDirectories: true)
}

func safeComponent(_ value: String) -> String {
    let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
    let scalars = value.unicodeScalars.map { allowed.contains($0) ? Character($0) : "-" }
    let text = String(scalars).trimmingCharacters(in: CharacterSet(charactersIn: "-"))
    return text.isEmpty ? "state" : text
}

func writeText(_ text: String, to path: String) throws {
    try ensureOutputDir()
    try text.write(toFile: path, atomically: true, encoding: .utf8)
}

func schema(_ properties: [String: Value], required: [String] = []) -> Value {
    var object: [String: Value] = [
        "type": .string("object"),
        "properties": .object(properties),
        "additionalProperties": .bool(false),
    ]
    if !required.isEmpty {
        object["required"] = .array(required.map { .string($0) })
    }
    return .object(object)
}

func prop(_ type: String, _ description: String, enumValues: [String]? = nil) -> Value {
    var object: [String: Value] = [
        "type": .string(type),
        "description": .string(description),
    ]
    if let enumValues {
        object["enum"] = .array(enumValues.map { .string($0) })
    }
    return .object(object)
}

func getRequiredString(from args: [String: Value]?, key: String) throws -> String {
    guard let value = args?[key]?.stringValue, !value.isEmpty else {
        throw MCPError.invalidParams("Missing or invalid required string argument: \(key)")
    }
    return value
}

func getOptionalString(from args: [String: Value]?, key: String) throws -> String? {
    guard let value = args?[key], !value.isNull else { return nil }
    guard let string = value.stringValue else {
        throw MCPError.invalidParams("Invalid string argument: \(key)")
    }
    return string
}

func getRequiredDouble(from args: [String: Value]?, key: String) throws -> Double {
    guard let value = args?[key] else {
        throw MCPError.invalidParams("Missing required number argument: \(key)")
    }
    if let double = value.doubleValue { return double }
    if let int = value.intValue { return Double(int) }
    if let string = value.stringValue, let double = Double(string) { return double }
    throw MCPError.invalidParams("Invalid number argument: \(key)")
}

func getOptionalDouble(from args: [String: Value]?, key: String) throws -> Double? {
    guard let value = args?[key], !value.isNull else { return nil }
    if let double = value.doubleValue { return double }
    if let int = value.intValue { return Double(int) }
    if let string = value.stringValue, let double = Double(string) { return double }
    throw MCPError.invalidParams("Invalid number argument: \(key)")
}

func getOptionalInt(from args: [String: Value]?, key: String) throws -> Int? {
    guard let value = args?[key], !value.isNull else { return nil }
    if let int = value.intValue { return int }
    if let double = value.doubleValue, let int = Int(exactly: double) { return int }
    if let string = value.stringValue, let int = Int(string) { return int }
    throw MCPError.invalidParams("Invalid integer argument: \(key)")
}

func parseCoordinateSpace(_ value: String?) throws -> String {
    let normalized = (value ?? "screenshot").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    guard coordinateSpaceEnum.contains(normalized) else {
        throw MCPError.invalidParams("Invalid coordinate_space \(normalized). Allowed: \(coordinateSpaceEnum.joined(separator: ", "))")
    }
    return normalized
}

func appKey(_ app: String) -> String {
    app.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
}

func axString(_ element: AXUIElement, _ attribute: String) -> String? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
          let value,
          CFGetTypeID(value) == CFStringGetTypeID() else {
        return nil
    }
    return value as? String
}

func axBool(_ element: AXUIElement, _ attribute: String) -> Bool? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success,
          let value,
          CFGetTypeID(value) == CFBooleanGetTypeID() else {
        return nil
    }
    return CFBooleanGetValue((value as! CFBoolean))
}

func axFrame(_ element: AXUIElement) -> CGRect? {
    var posValue: CFTypeRef?
    var sizeValue: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &posValue) == .success,
          AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeValue) == .success,
          let posValue,
          let sizeValue,
          CFGetTypeID(posValue) == AXValueGetTypeID(),
          CFGetTypeID(sizeValue) == AXValueGetTypeID() else {
        return nil
    }
    var point = CGPoint.zero
    var size = CGSize.zero
    AXValueGetValue(posValue as! AXValue, .cgPoint, &point)
    AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
    return CGRect(origin: point, size: size)
}

func axSupportedActions(_ element: AXUIElement) -> [String] {
    var actions: CFArray?
    guard AXUIElementCopyActionNames(element, &actions) == .success,
          let values = actions as? [String] else {
        return []
    }
    return values
}

func axIsValueSettable(_ element: AXUIElement) -> Bool {
    var settable = DarwinBoolean(false)
    let err = AXUIElementIsAttributeSettable(element, kAXValueAttribute as CFString, &settable)
    return err == .success && settable.boolValue
}

fileprivate enum AXPathSegment {
    case windows(Int)
    case mainWindow
    case children(Int)
}

func indexedSegment(_ value: String, prefix: String) -> Int? {
    let marker = "\(prefix)["
    guard value.hasPrefix(marker), value.hasSuffix("]") else { return nil }
    let start = value.index(value.startIndex, offsetBy: marker.count)
    let end = value.index(before: value.endIndex)
    return Int(value[start..<end])
}

fileprivate func parseAXPath(_ path: String) throws -> [AXPathSegment] {
    let parts = path.split(separator: ".").map(String.init)
    guard parts.first == "app" else {
        throw TactileMCPError.invalidArgument("AX path must start with app: \(path)")
    }
    return try parts.dropFirst().map { part in
        if part == "mainWindow" { return .mainWindow }
        if let index = indexedSegment(part, prefix: "windows") { return .windows(index) }
        if let index = indexedSegment(part, prefix: "children") { return .children(index) }
        throw TactileMCPError.invalidArgument("Unsupported AX path segment \(part) in \(path)")
    }
}

func childElement(of element: AXUIElement, at index: Int, path: String) throws -> AXUIElement {
    var childCount: CFIndex = 0
    let countErr = AXUIElementGetAttributeValueCount(element, kAXChildrenAttribute as CFString, &childCount)
    guard countErr == .success, index >= 0, index < childCount else {
        throw TactileMCPError.elementNotFound("Child index \(index) unavailable for \(path)")
    }
    var childrenRef: CFArray?
    let fetchErr = AXUIElementCopyAttributeValues(element, kAXChildrenAttribute as CFString, CFIndex(index), 1, &childrenRef)
    guard fetchErr == .success,
          let children = childrenRef as? [AXUIElement],
          let child = children.first else {
        throw TactileMCPError.elementNotFound("Could not fetch child index \(index) for \(path)")
    }
    return child
}

func windowElement(of element: AXUIElement, at index: Int, path: String) throws -> AXUIElement {
    var windowsValue: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(element, kAXWindowsAttribute as CFString, &windowsValue)
    guard err == .success,
          let windows = windowsValue as? [AXUIElement],
          index >= 0,
          index < windows.count else {
        throw TactileMCPError.elementNotFound("Window index \(index) unavailable for \(path)")
    }
    return windows[index]
}

func mainWindowElement(of element: AXUIElement, path: String) throws -> AXUIElement {
    var value: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(element, kAXMainWindowAttribute as CFString, &value)
    guard err == .success,
          let value,
          CFGetTypeID(value) == AXUIElementGetTypeID() else {
        throw TactileMCPError.elementNotFound("Main window unavailable for \(path)")
    }
    return value as! AXUIElement
}

func axElement(pid: Int32, path: String) throws -> AXUIElement {
    var current = AXUIElementCreateApplication(pid)
    for segment in try parseAXPath(path) {
        switch segment {
        case .windows(let index):
            current = try windowElement(of: current, at: index, path: path)
        case .mainWindow:
            current = try mainWindowElement(of: current, path: path)
        case .children(let index):
            current = try childElement(of: current, at: index, path: path)
        }
    }
    return current
}

func performAXAction(_ element: AXUIElement, action: String) throws {
    let supported = axSupportedActions(element)
    guard supported.contains(action) else {
        throw TactileMCPError.unsupportedAction("Element does not support \(action). Supported actions: \(supported.joined(separator: ", "))")
    }
    let err = AXUIElementPerformAction(element, action as CFString)
    guard err == .success else {
        throw TactileMCPError.actionFailed("AX action \(action) failed with AXError \(err.rawValue)")
    }
}

func setAXBoolAttribute(_ element: AXUIElement, attribute: String, value: Bool) throws {
    let cfValue: CFBoolean = value ? kCFBooleanTrue : kCFBooleanFalse
    let err = AXUIElementSetAttributeValue(element, attribute as CFString, cfValue)
    guard err == .success else {
        throw TactileMCPError.actionFailed("Setting \(attribute)=\(value) failed with AXError \(err.rawValue)")
    }
}

func setAXStringValue(_ element: AXUIElement, value: String) throws {
    let err = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFString)
    guard err == .success else {
        throw TactileMCPError.actionFailed("Setting AXValue failed with AXError \(err.rawValue)")
    }
}

func visibleWindowInfos(pid: pid_t) -> [WindowCaptureInfo] {
    guard let windowList = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
        return []
    }
    var windows: [WindowCaptureInfo] = []
    for window in windowList {
        guard let ownerPID = window[kCGWindowOwnerPID as String] as? pid_t,
              ownerPID == pid,
              let layer = window[kCGWindowLayer as String] as? Int,
              let windowID = window[kCGWindowNumber as String] as? CGWindowID,
              let bounds = window[kCGWindowBounds as String] as? NSDictionary else {
            continue
        }
        let alpha = window[kCGWindowAlpha as String] as? Double ?? 1.0
        var rect = CGRect.zero
        CGRectMakeWithDictionaryRepresentation(bounds as CFDictionary, &rect)
        guard alpha > 0.01,
              rect.width >= 8,
              rect.height >= 8,
              rect.width * rect.height >= 64 else {
            continue
        }
        windows.append(WindowCaptureInfo(windowID: windowID, frame: rect, layer: layer, alpha: alpha))
    }
    return windows.sorted {
        if $0.layer != $1.layer { return $0.layer < $1.layer }
        return $0.area > $1.area
    }
}

func getWindowInfo(pid: pid_t) -> WindowCaptureInfo? {
    let windows = visibleWindowInfos(pid: pid)
    return windows.first { $0.layer == 0 } ?? windows.first
}

func substantialWindows(from windows: [WindowCaptureInfo]) -> [WindowCaptureInfo] {
    windows.filter {
        $0.layer >= 0
            && $0.frame.width >= 20
            && $0.frame.height >= 20
            && $0.area >= 1_000
    }
}

func captureFrame(for windows: [WindowCaptureInfo]) -> CGRect? {
    let capturable = substantialWindows(from: windows)
    guard let first = capturable.first else { return nil }
    return capturable.dropFirst().reduce(first.frame) { partial, window in
        partial.union(window.frame)
    }
}

func shouldCaptureWindowUnion(_ windows: [WindowCaptureInfo]) -> Bool {
    substantialWindows(from: windows).count > 1
}

func imageSize(path: String) -> (Int, Int)? {
    guard let image = NSImage(contentsOfFile: path),
          let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff) else {
        return nil
    }
    return (rep.pixelsWide, rep.pixelsHigh)
}

func captureRegionScreenshot(frame: CGRect, path: String) -> Bool {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    let region = [
        Int(round(frame.minX)),
        Int(round(frame.minY)),
        Int(round(frame.width)),
        Int(round(frame.height)),
    ].map(String.init).joined(separator: ",")
    process.arguments = ["-x", "-R\(region)", path]
    process.standardOutput = Pipe()
    process.standardError = Pipe()
    do {
        try process.run()
        process.waitUntilExit()
    } catch {
        fputs("warning: failed to run screencapture fallback: \(error.localizedDescription)\n", stderr)
        return false
    }
    if process.terminationStatus != 0 {
        let stderrData = (process.standardError as? Pipe)?.fileHandleForReading.readDataToEndOfFile()
        let stderrText = stderrData.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        fputs("warning: screencapture fallback failed with status \(process.terminationStatus): \(stderrText)\n", stderr)
        return false
    }
    return imageSize(path: path) != nil
}

func captureScreenshot(pid: pid_t, basename: String, clickPoint: CGPoint? = nil, fallbackFrame: CGRect? = nil) -> ScreenshotInfo? {
    let path = "\(outputRoot)/\(basename).png"
    let windows = visibleWindowInfos(pid: pid)
    if shouldCaptureWindowUnion(windows),
       let unionFrame = captureFrame(for: windows),
       captureRegionScreenshot(frame: unionFrame, path: path),
       let size = imageSize(path: path) {
        return ScreenshotInfo(path: path, windowFrame: Frame(unionFrame), pixelWidth: size.0, pixelHeight: size.1)
    }

    guard let window = getWindowInfo(pid: pid) else {
        if let fallbackFrame,
           captureRegionScreenshot(frame: fallbackFrame, path: path),
           let size = imageSize(path: path) {
            return ScreenshotInfo(path: path, windowFrame: Frame(fallbackFrame), pixelWidth: size.0, pixelHeight: size.1)
        }
        return nil
    }
    let helperPath = ((CommandLine.arguments[0] as NSString).deletingLastPathComponent as NSString)
        .appendingPathComponent("screenshot-helper")
    guard FileManager.default.isExecutableFile(atPath: helperPath) else {
        fputs("warning: screenshot-helper not found at \(helperPath)\n", stderr)
        if captureRegionScreenshot(frame: window.frame, path: path),
           let size = imageSize(path: path) {
            return ScreenshotInfo(path: path, windowFrame: Frame(window.frame), pixelWidth: size.0, pixelHeight: size.1)
        }
        return nil
    }

    var args = [String(window.windowID), path]
    if let clickPoint {
        let rect = window.frame
        args += ["--click", "\(clickPoint.x),\(clickPoint.y)", "--bounds", "\(rect.minX),\(rect.minY),\(rect.width),\(rect.height)"]
    }

    let process = Process()
    process.executableURL = URL(fileURLWithPath: helperPath)
    process.arguments = args
    process.standardOutput = Pipe()
    process.standardError = Pipe()

    do {
        try process.run()
        process.waitUntilExit()
    } catch {
        fputs("warning: failed to run screenshot-helper: \(error.localizedDescription)\n", stderr)
        if captureRegionScreenshot(frame: window.frame, path: path),
           let size = imageSize(path: path) {
            return ScreenshotInfo(path: path, windowFrame: Frame(window.frame), pixelWidth: size.0, pixelHeight: size.1)
        }
        return nil
    }
    guard process.terminationStatus == 0, let size = imageSize(path: path) else {
        let stderrData = (process.standardError as? Pipe)?.fileHandleForReading.readDataToEndOfFile()
        let stderrText = stderrData.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        if !stderrText.isEmpty {
            fputs("warning: screenshot-helper failed with status \(process.terminationStatus): \(stderrText)\n", stderr)
        }
        if captureRegionScreenshot(frame: window.frame, path: path),
           let size = imageSize(path: path) {
            return ScreenshotInfo(path: path, windowFrame: Frame(window.frame), pixelWidth: size.0, pixelHeight: size.1)
        }
        return nil
    }
    return ScreenshotInfo(path: path, windowFrame: Frame(window.frame), pixelWidth: size.0, pixelHeight: size.1)
}

func parseObservationMode(_ value: String?) throws -> ObservationMode {
    let raw = value ?? ObservationMode.axOCR.rawValue
    guard let mode = ObservationMode(rawValue: raw) else {
        throw TactileMCPError.invalidArgument("Unsupported observation_mode \(raw). Allowed: \(observationModeEnum.joined(separator: ", "))")
    }
    return mode
}

func parseOCRLanguages(_ value: String?) -> [String] {
    (value ?? defaultOCRLanguages)
        .split(separator: ",")
        .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
        .filter { !$0.isEmpty }
}

func parseOCRRecognitionLevel(_ value: String?) throws -> String {
    let raw = value ?? "accurate"
    guard ocrRecognitionLevelEnum.contains(raw) else {
        throw TactileMCPError.invalidArgument("Unsupported ocr_recognition_level \(raw). Allowed: \(ocrRecognitionLevelEnum.joined(separator: ", "))")
    }
    return raw
}

func parseSummaryMode(_ value: String?) throws -> SummaryMode {
    let raw = value ?? SummaryMode.compact.rawValue
    guard let mode = SummaryMode(rawValue: raw) else {
        throw TactileMCPError.invalidArgument("Unsupported summary_mode \(raw). Allowed: \(summaryModeEnum.joined(separator: ", "))")
    }
    return mode
}

func parseSummaryOptions(from arguments: [String: Value]?) throws -> SummaryOptions {
    let mode = try parseSummaryMode(try getOptionalString(from: arguments, key: "summary_mode"))
    let defaultLimit = mode == .full ? Int.max : defaultSummaryElementLimit
    let elementLimit = try getOptionalInt(from: arguments, key: "element_limit") ?? defaultLimit
    guard elementLimit >= 0 else {
        throw TactileMCPError.invalidArgument("element_limit must be greater than or equal to 0")
    }
    let rawFilter = try getOptionalString(from: arguments, key: "element_filter")?
        .trimmingCharacters(in: .whitespacesAndNewlines)
    return SummaryOptions(
        mode: mode,
        elementLimit: elementLimit,
        elementFilter: rawFilter?.isEmpty == false ? rawFilter : nil
    )
}

func screenFrame(for imageFrame: Frame, screenshot: ScreenshotInfo) -> Frame {
    Frame(
        x: screenshot.windowFrame.x + imageFrame.x / screenshot.scaleX,
        y: screenshot.windowFrame.y + imageFrame.y / screenshot.scaleY,
        width: imageFrame.width / screenshot.scaleX,
        height: imageFrame.height / screenshot.scaleY
    )
}

func runOCR(imagePath: String, screenshot: ScreenshotInfo, languages: [String], recognitionLevel: String) throws -> OCRPayload {
    guard let image = NSImage(contentsOfFile: imagePath),
          let tiff = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiff),
          let cgImage = bitmap.cgImage else {
        throw TactileMCPError.actionFailed("Failed to load screenshot for OCR: \(imagePath)")
    }

    let imageWidth = Double(cgImage.width)
    let imageHeight = Double(cgImage.height)
    var lines: [OCRLine] = []
    var requestError: Error?

    let request = VNRecognizeTextRequest { request, error in
        requestError = error
        let observations = (request.results as? [VNRecognizedTextObservation]) ?? []
        for observation in observations {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let box = observation.boundingBox
            let imageFrame = Frame(
                x: Double(box.minX) * imageWidth,
                y: Double(1.0 - box.maxY) * imageHeight,
                width: Double(box.width) * imageWidth,
                height: Double(box.height) * imageHeight
            )
            let frame = screenFrame(for: imageFrame, screenshot: screenshot)
            lines.append(
                OCRLine(
                    text: candidate.string,
                    confidence: Double(candidate.confidence),
                    frame: imageFrame,
                    imageFrame: imageFrame,
                    screenFrame: frame,
                    screenCenter: PointInfo(x: frame.x + frame.width / 2, y: frame.y + frame.height / 2)
                )
            )
        }
    }

    request.recognitionLevel = recognitionLevel == "fast" ? .fast : .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = languages
    if #available(macOS 13.0, *) {
        request.revision = VNRecognizeTextRequestRevision3
    }

    do {
        try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
    } catch {
        throw TactileMCPError.actionFailed("OCR failed: \(error.localizedDescription)")
    }
    if let requestError {
        throw TactileMCPError.actionFailed("OCR request failed: \(requestError.localizedDescription)")
    }

    lines.sort {
        if abs($0.frame.y - $1.frame.y) > 4 {
            return $0.frame.y < $1.frame.y
        }
        return $0.frame.x < $1.frame.x
    }

    return OCRPayload(
        image: imagePath,
        imageWidth: Int(imageWidth),
        imageHeight: Int(imageHeight),
        languages: languages,
        recognitionLevel: recognitionLevel,
        lines: lines,
        source: OCRSource(kind: "window_screenshot", region: screenshot.windowFrame, screenshot: imagePath),
        coordinateSpace: OCRCoordinateSpace(
            frame: "image_pixels_relative_to_screenshot",
            imageFrame: "image_pixels_relative_to_screenshot",
            screenFrame: "screen_points_top_left"
        )
    )
}

func ocrElements(from payload: OCRPayload) -> [IndexedElement] {
    payload.lines.enumerated().map { index, line in
        IndexedElement(
            index: "o\(index)",
            source: "ocr",
            role: "OCRLine",
            text: line.text,
            screenFrame: line.screenFrame,
            screenshotFrame: line.imageFrame,
            screenCenter: line.screenCenter,
            screenshotCenter: center(of: line.imageFrame),
            confidence: line.confidence,
            axPath: nil,
            settable: false,
            focused: nil,
            selected: nil,
            secondaryActions: []
        )
    }
}

func visualObservation(for mode: ObservationMode, screenshot: ScreenshotInfo?, error: String? = nil) -> VisualObservation? {
    guard mode.includesVisual else { return nil }
    guard let screenshot else {
        return VisualObservation(
            enabled: true,
            imageAttachedToToolResult: false,
            screenshotPath: nil,
            coordinateSpace: nil,
            error: error ?? "Visual observation requested, but screenshot capture failed."
        )
    }
    return VisualObservation(
        enabled: true,
        imageAttachedToToolResult: true,
        screenshotPath: screenshot.path,
        coordinateSpace: VisualCoordinateSpace(
            frame: "screen_points_top_left",
            screenshotRegion: screenshot.windowFrame,
            screenshotPixels: PointInfo(x: Double(screenshot.pixelWidth), y: Double(screenshot.pixelHeight)),
            rule: "Visual model should use the attached screenshot for reasoning. Raw click x/y, scroll, and drag coordinates default to screenshot pixels from this image; click can also use coordinate_space=screen or screen_x/screen_y for macOS screen points."
        ),
        error: error
    )
}

func screenshotPointToScreen(_ x: Double, _ y: Double, state: AppState) -> CGPoint {
    guard let screenshot = state.screenshot else {
        return CGPoint(x: x, y: y)
    }
    return CGPoint(
        x: screenshot.windowFrame.x + x / screenshot.scaleX,
        y: screenshot.windowFrame.y + y / screenshot.scaleY
    )
}

func screenPointToScreenshot(_ point: CGPoint, screenshot: ScreenshotInfo?) -> PointInfo? {
    guard let screenshot else { return nil }
    return PointInfo(
        x: (point.x - screenshot.windowFrame.x) * screenshot.scaleX,
        y: (point.y - screenshot.windowFrame.y) * screenshot.scaleY
    )
}

func screenFrameToScreenshot(_ frame: Frame?, screenshot: ScreenshotInfo?) -> Frame? {
    guard let frame, let screenshot else { return nil }
    return Frame(
        x: (frame.x - screenshot.windowFrame.x) * screenshot.scaleX,
        y: (frame.y - screenshot.windowFrame.y) * screenshot.scaleY,
        width: frame.width * screenshot.scaleX,
        height: frame.height * screenshot.scaleY
    )
}

func center(of frame: Frame?) -> PointInfo? {
    guard let frame else { return nil }
    return PointInfo(x: frame.x + frame.width / 2, y: frame.y + frame.height / 2)
}

func elementCenter(_ element: IndexedElement) throws -> CGPoint {
    guard let center = element.screenCenter else {
        throw TactileMCPError.elementNotFound("Element \(element.index) has no screenCenter")
    }
    return CGPoint(x: center.x, y: center.y)
}

func isTextInputElement(_ element: IndexedElement) -> Bool {
    element.role.contains("AXTextArea")
        || element.role.contains("AXTextField")
        || element.role.contains("AXComboBox")
        || element.role.contains("AXSearchField")
}

func focusOrClickElement(_ element: IndexedElement, state: AppState) {
    var didFocus = false
    if let path = element.axPath {
        do {
            try MacosUseSDK.focusAccessibilityElement(pid: state.pid, atPath: path)
            didFocus = true
        } catch {
            fputs("warning: AX focus failed before text replacement: \(error.localizedDescription)\n", stderr)
        }
    }
    if !didFocus, let center = try? elementCenter(element) {
        do {
            try MacosUseSDK.clickMouse(at: center)
        } catch {
            fputs("warning: click focus failed before text replacement: \(error.localizedDescription)\n", stderr)
        }
    }
}

func replaceFocusedText(with value: String) throws {
    try pressKeyCombo("cmd+a")
    if value.isEmpty {
        try pressKeyCombo("delete")
    } else {
        try MacosUseSDK.writeText(value)
    }
}

func clippedSummaryText(_ text: String, maxLength: Int) -> String {
    let normalized = text
        .replacingOccurrences(of: "\r", with: " ")
        .replacingOccurrences(of: "\n", with: " ")
    guard normalized.count > maxLength else { return normalized }
    let end = normalized.index(normalized.startIndex, offsetBy: maxLength)
    return String(normalized[..<end]) + "..."
}

func describeElement(_ element: IndexedElement, maxTextLength: Int? = nil) -> String {
    var parts: [String] = ["\(element.index) \(element.role)"]
    if element.source != "ax" {
        parts.append("[\(element.source)]")
    }
    if let text = element.text, !text.isEmpty {
        let normalized = text.replacingOccurrences(of: "\n", with: " ")
        if let maxTextLength {
            parts.append(clippedSummaryText(normalized, maxLength: maxTextLength))
        } else {
            parts.append(normalized)
        }
    }
    if let confidence = element.confidence {
        parts.append("confidence:\(String(format: "%.2f", confidence))")
    }
    if element.settable {
        parts.append("(settable)")
    }
    if let frame = element.screenFrame {
        parts.append("screenFrame:x:\(Int(frame.x)) y:\(Int(frame.y)) w:\(Int(frame.width)) h:\(Int(frame.height))")
    }
    if let center = element.screenCenter {
        parts.append("screenCenter:x:\(Int(center.x)) y:\(Int(center.y))")
    }
    if let screenshotFrame = element.screenshotFrame {
        parts.append("screenshotFrame:x:\(Int(screenshotFrame.x)) y:\(Int(screenshotFrame.y)) w:\(Int(screenshotFrame.width)) h:\(Int(screenshotFrame.height))")
    }
    if let screenshotCenter = element.screenshotCenter {
        parts.append("screenshotCenter:x:\(Int(screenshotCenter.x)) y:\(Int(screenshotCenter.y))")
    }
    if !element.secondaryActions.isEmpty {
        parts.append("Secondary Actions: \(element.secondaryActions.joined(separator: ", "))")
    }
    return parts.joined(separator: " ")
}

func hasVisibleFrame(_ element: IndexedElement) -> Bool {
    guard let frame = element.screenFrame else { return false }
    return frame.width > 0 && frame.height > 0
}

func isMenuElement(_ element: IndexedElement) -> Bool {
    element.role.contains("AXMenuBar") || element.role.contains("AXMenuBarItem")
}

func isInteractiveElement(_ element: IndexedElement) -> Bool {
    let roles = [
        "AXButton", "AXRadioButton", "AXCheckBox", "AXPopUpButton", "AXComboBox",
        "AXTextField", "AXTextArea", "AXLink", "AXMenuButton", "AXColorWell",
        "AXSlider", "AXScrollArea", "AXTabGroup", "AXWebArea",
    ]
    if roles.contains(where: { element.role.contains($0) }) {
        return true
    }
    return element.secondaryActions.contains(where: {
        ["AXPress", "AXConfirm", "AXShowMenu", "AXIncrement", "AXDecrement"].contains($0)
    })
}

func isControlElement(_ element: IndexedElement) -> Bool {
    let roles = [
        "AXButton", "AXRadioButton", "AXCheckBox", "AXPopUpButton", "AXComboBox",
        "AXTextField", "AXTextArea", "AXLink", "AXMenuButton", "AXColorWell",
        "AXSlider",
    ]
    return roles.contains(where: { element.role.contains($0) })
}

func normalizedSearchText(_ value: String) -> String {
    value
        .components(separatedBy: .whitespacesAndNewlines)
        .joined()
        .lowercased()
}

func isOverlayLikeElement(_ element: IndexedElement, in state: AppState) -> Bool {
    guard hasVisibleFrame(element), !isMenuElement(element) else { return false }
    let role = element.role.lowercased()
    let text = (element.text ?? "").lowercased()
    let searchable = normalizedSearchText("\(element.role) \(element.text ?? "")")

    if role.contains("axwindow") {
        if text.isEmpty { return true }
        if let title = state.windowTitle, normalizedSearchText(text) == normalizedSearchText(title) {
            return false
        }
        return true
    }

    let overlayMarkers = [
        "dialog", "popover", "popup", "sheet", "drawer", "panel",
        "对话框", "弹窗", "弹出", "浮层", "面板", "抽屉",
    ]
    if overlayMarkers.contains(where: { searchable.contains($0) }) {
        return true
    }

    let resultMarkers = [
        "searchresult", "searchresults", "resultlist", "results",
        "搜索结果", "搜索建议", "候选", "结果",
    ]
    if (role.contains("axtable") || role.contains("axlist") || role.contains("axgroup"))
        && resultMarkers.contains(where: { searchable.contains($0) }) {
        return true
    }

    return false
}

func overlayElements(for state: AppState) -> [IndexedElement] {
    state.elements
        .filter { $0.source == "ax" && isOverlayLikeElement($0, in: state) }
        .sorted {
            if $0.role.contains("AXWindow") != $1.role.contains("AXWindow") {
                return $0.role.contains("AXWindow")
            }
            return screenOrder($0, $1)
        }
}

func hasUsefulText(_ element: IndexedElement) -> Bool {
    guard let text = element.text?.trimmingCharacters(in: .whitespacesAndNewlines),
          !text.isEmpty else {
        return false
    }
    let genericSuffixes = ["View", "Button", "Widget", "Delegate", "Contents"]
    return !genericSuffixes.contains(where: { text.hasSuffix($0) })
}

func screenOrder(_ lhs: IndexedElement, _ rhs: IndexedElement) -> Bool {
    let left = lhs.screenFrame ?? Frame(x: 0, y: 0, width: 0, height: 0)
    let right = rhs.screenFrame ?? Frame(x: 0, y: 0, width: 0, height: 0)
    if abs(left.y - right.y) > 4 {
        return left.y < right.y
    }
    if abs(left.x - right.x) > 4 {
        return left.x < right.x
    }
    return lhs.index < rhs.index
}

func normalizedElementText(_ text: String?) -> String {
    guard let text else { return "" }
    return text
        .components(separatedBy: .whitespacesAndNewlines)
        .joined()
        .lowercased()
}

func pointDistance(_ lhs: PointInfo?, _ rhs: PointInfo?) -> Double? {
    guard let lhs, let rhs else { return nil }
    let dx = lhs.x - rhs.x
    let dy = lhs.y - rhs.y
    return (dx * dx + dy * dy).squareRoot()
}

func relocationRadius(for element: IndexedElement) -> Double {
    guard let frame = element.screenFrame else { return 120 }
    return max(120, max(frame.width, frame.height) * 2)
}

func relocationScore(original: IndexedElement, candidate: IndexedElement) -> Double? {
    guard candidate.source == "ax", candidate.role == original.role else { return nil }
    let originalText = normalizedElementText(original.text)
    let candidateText = normalizedElementText(candidate.text)
    if !originalText.isEmpty && candidateText != originalText {
        return nil
    }
    let distance = pointDistance(original.screenCenter, candidate.screenCenter)
    if original.screenCenter != nil && distance == nil {
        return nil
    }
    if let distance, distance > relocationRadius(for: original) {
        return nil
    }

    var score = distance ?? 1000
    if let originalFrame = original.screenFrame, let candidateFrame = candidate.screenFrame {
        let widthDelta = abs(originalFrame.width - candidateFrame.width)
        let heightDelta = abs(originalFrame.height - candidateFrame.height)
        score += (widthDelta + heightDelta) * 0.1
    } else if original.screenFrame != nil {
        return nil
    }
    if candidate.index == original.index {
        score -= 25
    }
    if originalText.isEmpty && candidateText.isEmpty {
        score += 10
    }
    return score
}

func bestRelocatedElement(original: IndexedElement, in elements: [IndexedElement]) -> IndexedElement? {
    elements
        .compactMap { candidate -> (IndexedElement, Double)? in
            guard let score = relocationScore(original: original, candidate: candidate) else { return nil }
            return (candidate, score)
        }
        .sorted {
            if abs($0.1 - $1.1) > 0.001 {
                return $0.1 < $1.1
            }
            return $0.0.index < $1.0.index
        }
        .first?
        .0
}

func isStaleAXPathError(_ error: Error) -> Bool {
    guard case TactileMCPError.elementNotFound(let message) = error else { return false }
    return message.contains("Window index")
        || message.contains("Child index")
        || message.contains("Could not fetch child index")
}

func keyElements(for state: AppState, limit: Int = 80) -> [IndexedElement] {
    var seen = Set<String>()
    var result: [IndexedElement] = []

    func append(_ elements: [IndexedElement]) {
        for element in elements where result.count < limit {
            if seen.insert(element.index).inserted {
                result.append(element)
            }
        }
    }

    if let focused = state.focusedElementIndex,
       let element = state.elements.first(where: { $0.index == focused }) {
        append([element])
    }

    append(overlayElements(for: state))

    let namedControls = state.elements
        .filter { hasVisibleFrame($0) && !isMenuElement($0) && isControlElement($0) && hasUsefulText($0) }
        .sorted(by: screenOrder)
    append(namedControls)

    let textInputs = state.elements
        .filter {
            hasVisibleFrame($0) && !isMenuElement($0)
                && ($0.role.contains("AXTextField") || $0.role.contains("AXTextArea") || $0.role.contains("AXComboBox"))
        }
        .sorted(by: screenOrder)
    append(textInputs)

    let actionable = state.elements
        .filter { hasVisibleFrame($0) && !isMenuElement($0) && isInteractiveElement($0) && !isControlElement($0) }
        .sorted(by: screenOrder)
    append(actionable)

    let visibleText = state.elements
        .filter { hasVisibleFrame($0) && !isMenuElement($0) && hasUsefulText($0) }
        .sorted(by: screenOrder)
    append(visibleText)

    if result.isEmpty {
        append(Array(state.elements.prefix(limit)))
    }
    return result
}

func modelElements(for state: AppState) -> [IndexedElement] {
    state.elements.sorted(by: screenOrder)
}

func textMatchesFilter(_ text: String, filter: String) -> Bool {
    guard !text.isEmpty else { return false }
    if let regex = try? NSRegularExpression(pattern: filter, options: [.caseInsensitive]) {
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        return regex.firstMatch(in: text, range: range) != nil
    }
    return text.range(of: filter, options: [.caseInsensitive, .diacriticInsensitive]) != nil
}

func elementMatchesFilter(_ element: IndexedElement, filter: String) -> Bool {
    let haystacks = [
        element.index,
        element.source,
        element.role,
        element.text ?? "",
        element.axPath ?? "",
        element.secondaryActions.joined(separator: " "),
        element.settable ? "settable" : "",
        element.focused == true ? "focused" : "",
        element.selected == true ? "selected" : "",
    ]
    return haystacks.contains { textMatchesFilter($0, filter: filter) }
}

func limitedElements(_ elements: [IndexedElement], limit: Int) -> [IndexedElement] {
    if limit == Int.max {
        return elements
    }
    return Array(elements.prefix(limit))
}

func summaryElements(for state: AppState, options: SummaryOptions) -> [IndexedElement] {
    guard options.mode != .metadata else { return [] }
    if let filter = options.elementFilter {
        let filtered = modelElements(for: state).filter { elementMatchesFilter($0, filter: filter) }
        return limitedElements(filtered, limit: options.elementLimit)
    }
    switch options.mode {
    case .compact:
        return keyElements(for: state, limit: options.elementLimit)
    case .full:
        return limitedElements(modelElements(for: state), limit: options.elementLimit)
    case .metadata:
        return []
    }
}

func countElements(in elements: [IndexedElement], source: String) -> Int {
    elements.filter { $0.source == source }.count
}

func summaryScopeElements(for state: AppState, options: SummaryOptions) -> [IndexedElement] {
    if let filter = options.elementFilter {
        return state.elements.filter { elementMatchesFilter($0, filter: filter) }
    }
    return state.elements
}

func flatStateText(_ state: AppState) -> String {
    var lines: [String] = []
    lines.append("# app: \(state.appName)")
    lines.append("# requested_app: \(state.requestedApp)")
    lines.append("# pid: \(state.pid)")
    lines.append("# observation_mode: \(state.observationMode.rawValue)")
    if let bundle = state.bundleIdentifier { lines.append("# bundle: \(bundle)") }
    if let title = state.windowTitle { lines.append("# window: \(title)") }
    if let screenshot = state.screenshot {
        lines.append("# screenshot: \(screenshot.path)")
        lines.append("# screenshot_pixels: \(screenshot.pixelWidth)x\(screenshot.pixelHeight)")
        lines.append("# window_frame: x:\(Int(screenshot.windowFrame.x)) y:\(Int(screenshot.windowFrame.y)) w:\(Int(screenshot.windowFrame.width)) h:\(Int(screenshot.windowFrame.height))")
    }
    if let error = state.ocrError {
        lines.append("# ocr_error: \(error)")
    }
    lines.append("# ax_elements:")
    for element in state.elements.filter({ $0.source == "ax" }) {
        lines.append(describeElement(element))
        if let path = element.axPath {
            lines.append("  axPath: \(path)")
        }
    }
    let ocrElements = state.elements.filter { $0.source == "ocr" }
    if !ocrElements.isEmpty {
        lines.append("# ocr_lines:")
        for element in ocrElements {
            lines.append(describeElement(element))
        }
    }
    return lines.joined(separator: "\n") + "\n"
}

func compactStateSummary(_ state: AppState, prefix: String, options: SummaryOptions = SummaryOptions()) -> String {
    var lines: [String] = []
    lines.append(prefix)
    lines.append("App=\(state.bundleIdentifier ?? state.appName) (pid \(state.pid))")
    if let title = state.windowTitle {
        lines.append("Window: \"\(title)\", App: \(state.appName).")
    }
    if let path = state.statePath {
        lines.append("state: \(path)")
    }
    if let path = state.textPath {
        lines.append("elements: \(path)")
    }
    if let screenshot = state.screenshot {
        lines.append("screenshot: \(screenshot.path)")
    }
    lines.append("observation_mode: \(state.observationMode.rawValue)")
    lines.append("summary_mode: \(options.mode.rawValue)")
    if options.elementLimit != Int.max {
        lines.append("element_limit: \(options.elementLimit)")
    }
    if let filter = options.elementFilter {
        lines.append("element_filter: \(filter)")
    }
    lines.append("total_elements: \(state.elements.count)")
    if let payload = state.ocrPayload {
        lines.append("ocr_lines: \(payload.lines.count)")
    }
    if let error = state.ocrError {
        lines.append("ocr_error: \(error)")
    }
    if let visual = state.visualObservation {
        lines.append("visual_observation: enabled=\(visual.enabled) image_attached_to_tool_result=\(visual.imageAttachedToToolResult)")
    }
    if options.mode != .full {
        let artifacts = state.textPath ?? state.statePath ?? outputRoot
        lines.append("full_element_dump: \(artifacts)")
    }
    lines.append("")
    let scope = summaryScopeElements(for: state, options: options)
    let shown = summaryElements(for: state, options: options)
    if options.mode == .metadata {
        lines.append("Element listing omitted by summary_mode=metadata.")
    } else if options.elementFilter != nil && scope.isEmpty {
        lines.append("No elements matched element_filter.")
    }
    let axElements = shown.filter { $0.source == "ax" }
    let axScopeCount = countElements(in: scope, source: "ax")
    if options.mode != .metadata || !axElements.isEmpty {
        lines.append("AX elements (showing \(axElements.count) of \(axScopeCount), sorted by screen position):")
        for element in axElements {
            lines.append(describeElement(element, maxTextLength: summaryTextMaxLength))
        }
        let omitted = max(0, axScopeCount - axElements.count)
        if omitted > 0 {
            lines.append("... omitted \(omitted) AX elements. See full_element_dump for the complete list.")
        }
    }
    if options.mode == .compact && options.elementFilter == nil {
        let overlays = overlayElements(for: state)
        if !overlays.isEmpty {
            lines.append("")
            lines.append("Overlay/search-result candidates (prioritize these before background content):")
            for element in overlays.prefix(12) {
                lines.append(describeElement(element, maxTextLength: summaryTextMaxLength))
            }
            if overlays.count > 12 {
                lines.append("... omitted \(overlays.count - 12) overlay candidates. See full_element_dump for the complete list.")
            }
        }
    }
    let ocrElements = shown.filter { $0.source == "ocr" }
    let ocrScopeCount = countElements(in: scope, source: "ocr")
    if options.mode != .metadata && (!ocrElements.isEmpty || ocrScopeCount > 0) {
        lines.append("")
        lines.append("OCR lines (showing \(ocrElements.count) of \(ocrScopeCount), coordinate-backed; prefer AX elements first):")
        for element in ocrElements {
            lines.append(describeElement(element, maxTextLength: summaryTextMaxLength))
        }
        let omitted = max(0, ocrScopeCount - ocrElements.count)
        if omitted > 0 {
            lines.append("... omitted \(omitted) OCR lines. See full_element_dump for the complete list.")
        }
    }
    if let focused = state.focusedElementIndex,
       let element = state.elements.first(where: { $0.index == focused }) {
        lines.append("")
        lines.append("Focused element:")
        lines.append(describeElement(element, maxTextLength: summaryTextMaxLength))
    }
    return lines.joined(separator: "\n")
}

func writeArtifacts(for state: inout AppState, basename: String) throws {
    try ensureOutputDir()
    let statePath = "\(outputRoot)/\(basename).json"
    let textPath = "\(outputRoot)/\(basename).txt"
    state.statePath = statePath
    state.textPath = textPath
    try writeText(try jsonString(state), to: statePath)
    try writeText(flatStateText(state), to: textPath)
}

func applicationBundleInfo(pid: pid_t) -> (String?, String?) {
    guard let app = NSRunningApplication(processIdentifier: pid) else { return (nil, nil) }
    return (app.bundleIdentifier, app.localizedName)
}

func windowTitle(from traversal: ResponseData) -> String? {
    traversal.elements.first { $0.role.contains("AXWindow") && ($0.text?.isEmpty == false) }?.text
}

func windowFrame(from traversal: ResponseData) -> CGRect? {
    traversal.elements.first {
        $0.role.contains("AXWindow")
            && $0.x != nil
            && $0.y != nil
            && $0.width != nil
            && $0.height != nil
    }.flatMap { element in
        guard let x = element.x, let y = element.y, let width = element.width, let height = element.height else {
            return nil
        }
        return CGRect(x: x, y: y, width: width, height: height)
    }
}

func indexedElements(from traversal: ResponseData, pid: Int32, screenshot: ScreenshotInfo?) -> [IndexedElement] {
    traversal.elements.enumerated().map { index, element in
        let screenFrame: Frame?
        if let x = element.x, let y = element.y, let width = element.width, let height = element.height {
            screenFrame = Frame(x: x, y: y, width: width, height: height)
        } else {
            screenFrame = nil
        }
        let screenCenter = center(of: screenFrame)
        let screenshotCenter: PointInfo?
        if let screenCenter {
            screenshotCenter = screenPointToScreenshot(CGPoint(x: screenCenter.x, y: screenCenter.y), screenshot: screenshot)
        } else {
            screenshotCenter = nil
        }
        return IndexedElement(
            index: String(index),
            role: element.role,
            text: element.text,
            screenFrame: screenFrame,
            screenshotFrame: screenFrameToScreenshot(screenFrame, screenshot: screenshot),
            screenCenter: screenCenter,
            screenshotCenter: screenshotCenter,
            axPath: element.axPath,
            settable: element.isSettable ?? false,
            focused: element.isFocused,
            selected: element.isSelected,
            secondaryActions: element.axActions ?? []
        )
    }
}

func normalizeKeyCombo(_ combo: String) throws -> (String, CGEventFlags) {
    let cleaned = combo
        .replacingOccurrences(of: "super", with: "cmd", options: .caseInsensitive)
        .replacingOccurrences(of: "command", with: "cmd", options: .caseInsensitive)
        .replacingOccurrences(of: "control", with: "ctrl", options: .caseInsensitive)
        .replacingOccurrences(of: "option", with: "alt", options: .caseInsensitive)
        .replacingOccurrences(of: " ", with: "")
    let parts = cleaned.split(separator: "+").map { String($0).lowercased() }
    guard let key = parts.last else {
        throw TactileMCPError.invalidArgument("Invalid key combination: \(combo)")
    }
    var flags: CGEventFlags = []
    for modifier in parts.dropLast() {
        switch modifier {
        case "cmd", "meta":
            flags.insert(.maskCommand)
        case "shift":
            flags.insert(.maskShift)
        case "ctrl":
            flags.insert(.maskControl)
        case "alt", "opt":
            flags.insert(.maskAlternate)
        case "fn", "function":
            flags.insert(.maskSecondaryFn)
        default:
            throw TactileMCPError.invalidArgument("Unknown key modifier: \(modifier)")
        }
    }
    return (key, flags)
}

func keyCode(for key: String) -> CGKeyCode? {
    let lower = key.lowercased()
    if lower.hasPrefix("kp_") {
        switch lower {
        case "kp_0": return 82
        case "kp_1": return 83
        case "kp_2": return 84
        case "kp_3": return 85
        case "kp_4": return 86
        case "kp_5": return 87
        case "kp_6": return 88
        case "kp_7": return 89
        case "kp_8": return 91
        case "kp_9": return 92
        case "kp_enter": return 76
        case "kp_decimal": return 65
        case "kp_add": return 69
        case "kp_subtract": return 78
        case "kp_multiply": return 67
        case "kp_divide": return 75
        default: return nil
        }
    }
    switch lower {
    case "up", "arrowup": return MacosUseSDK.mapKeyNameToKeyCode("up")
    case "down", "arrowdown": return MacosUseSDK.mapKeyNameToKeyCode("down")
    case "left", "arrowleft": return MacosUseSDK.mapKeyNameToKeyCode("left")
    case "right", "arrowright": return MacosUseSDK.mapKeyNameToKeyCode("right")
    default: return MacosUseSDK.mapKeyNameToKeyCode(lower)
    }
}

func pressKeyCombo(_ combo: String) throws {
    let (key, flags) = try normalizeKeyCombo(combo)
    guard let code = keyCode(for: key) else {
        throw TactileMCPError.invalidArgument("Unknown key: \(key)")
    }
    try MacosUseSDK.pressKey(keyCode: code, flags: flags)
}

func performRawClick(at point: CGPoint, button: String, count: Int) throws {
    let count = max(1, min(count, 5))
    switch button {
    case "left":
        if count == 2 {
            try MacosUseSDK.doubleClickMouse(at: point)
        } else {
            for _ in 0..<count { try MacosUseSDK.clickMouse(at: point) }
        }
    case "right":
        for _ in 0..<count { try MacosUseSDK.rightClickMouse(at: point) }
    case "middle":
        let source = CGEventSource(stateID: .hidSystemState)
        for _ in 0..<count {
            let down = CGEvent(mouseEventSource: source, mouseType: .otherMouseDown, mouseCursorPosition: point, mouseButton: .center)
            let up = CGEvent(mouseEventSource: source, mouseType: .otherMouseUp, mouseCursorPosition: point, mouseButton: .center)
            down?.post(tap: .cghidEventTap)
            usleep(15_000)
            up?.post(tap: .cghidEventTap)
            usleep(15_000)
        }
    default:
        throw TactileMCPError.invalidArgument("Unsupported mouse_button: \(button)")
    }
}

func performDrag(from start: CGPoint, to end: CGPoint) throws {
    guard let source = CGEventSource(stateID: .hidSystemState) else {
        throw TactileMCPError.actionFailed("Failed to create CGEventSource")
    }
    CGWarpMouseCursorPosition(start)
    CGAssociateMouseAndMouseCursorPosition(boolean_t(1))
    let down = CGEvent(mouseEventSource: source, mouseType: .leftMouseDown, mouseCursorPosition: start, mouseButton: .left)
    down?.post(tap: .cghidEventTap)
    usleep(20_000)
    let steps = 16
    for i in 1...steps {
        let t = CGFloat(i) / CGFloat(steps)
        let point = CGPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t)
        let drag = CGEvent(mouseEventSource: source, mouseType: .leftMouseDragged, mouseCursorPosition: point, mouseButton: .left)
        drag?.post(tap: .cghidEventTap)
        usleep(10_000)
    }
    let up = CGEvent(mouseEventSource: source, mouseType: .leftMouseUp, mouseCursorPosition: end, mouseButton: .left)
    up?.post(tap: .cghidEventTap)
    usleep(20_000)
}

struct ClipboardSnapshot {
    let items: [[NSPasteboard.PasteboardType: Data]]

    static func capture() -> ClipboardSnapshot {
        let pasteboard = NSPasteboard.general
        let items: [[NSPasteboard.PasteboardType: Data]] = pasteboard.pasteboardItems?.map { item -> [NSPasteboard.PasteboardType: Data] in
            var values: [NSPasteboard.PasteboardType: Data] = [:]
            for type in item.types {
                if let data = item.data(forType: type) {
                    values[type] = data
                }
            }
            return values
        } ?? []
        return ClipboardSnapshot(items: items)
    }

    func restore() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        let restored = items.map { values -> NSPasteboardItem in
            let item = NSPasteboardItem()
            for (type, data) in values {
                item.setData(data, forType: type)
            }
            return item
        }
        if !restored.isEmpty {
            pasteboard.writeObjects(restored)
        }
    }
}

func pasteTextWithClipboard(_ text: String) throws {
    let snapshot = ClipboardSnapshot.capture()
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    pasteboard.setString(text, forType: .string)
    try pressKeyCombo("cmd+v")
    usleep(80_000)
    snapshot.restore()
}

func normalizedAppURL(_ url: URL?) -> URL? {
    url?.standardizedFileURL.resolvingSymlinksInPath()
}

func preferredRunningApplication(from candidates: [NSRunningApplication]) -> NSRunningApplication? {
    candidates.sorted {
        if $0.isActive != $1.isActive { return $0.isActive && !$1.isActive }
        return $0.processIdentifier < $1.processIdentifier
    }.first
}

func runningApplication(for identifier: String) -> NSRunningApplication? {
    let trimmed = identifier.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return nil }

    if trimmed.hasSuffix(".app"), trimmed.contains("/") {
        let targetURL = normalizedAppURL(URL(fileURLWithPath: trimmed))
        let candidates = NSWorkspace.shared.runningApplications.filter {
            normalizedAppURL($0.bundleURL) == targetURL
        }
        return preferredRunningApplication(from: candidates)
    }

    let bundleMatches = NSRunningApplication.runningApplications(withBundleIdentifier: trimmed)
    if let match = preferredRunningApplication(from: bundleMatches) {
        return match
    }

    let lowered = trimmed.lowercased()
    let candidates = NSWorkspace.shared.runningApplications.filter { app in
        if let name = app.localizedName?.lowercased(), name == lowered {
            return true
        }
        if let bundleID = app.bundleIdentifier?.lowercased(), bundleID == lowered {
            return true
        }
        if let bundleName = app.bundleURL?.deletingPathExtension().lastPathComponent.lowercased(), bundleName == lowered {
            return true
        }
        return false
    }
    return preferredRunningApplication(from: candidates)
}

final class StateManager {
    private var states: [String: AppState] = [:]

    private func state(_ state: AppState, satisfies mode: ObservationMode) -> Bool {
        switch mode {
        case .ax:
            return true
        case .axOCR:
            return state.observationMode.includesOCR
        case .axOCRVisual:
            return state.observationMode.includesVisual
        }
    }

    @MainActor
    func state(
        for app: String,
        forceRefresh: Bool = false,
        clickPoint: CGPoint? = nil,
        observationMode: ObservationMode = .axOCR,
        ocrLanguages: [String] = ["zh-Hans", "en-US"],
        ocrRecognitionLevel: String = "accurate"
    ) async throws -> AppState {
        let key = appKey(app)
        if !forceRefresh, let cached = states[key], state(cached, satisfies: observationMode) {
            return cached
        }

        let initialRunningApp = runningApplication(for: app)
        let pid: Int32
        let launchedAppName: String?
        let didLaunchApp: Bool

        if let initialRunningApp {
            pid = initialRunningApp.processIdentifier
            launchedAppName = nil
            didLaunchApp = false
        } else {
            let openResult = try await MacosUseSDK.openApplication(identifier: app)
            guard let launchedPID = Int32(exactly: openResult.pid) else {
                throw TactileMCPError.appStateUnavailable("PID \(openResult.pid) is out of Int32 range")
            }
            pid = launchedPID
            launchedAppName = openResult.appName
            didLaunchApp = true
        }
        let runningApp = NSRunningApplication(processIdentifier: pid_t(pid))
        if didLaunchApp {
            try? await Task.sleep(nanoseconds: 120_000_000)
        }

        let traversal = try MacosUseSDK.traverseAccessibilityTree(pid: pid, onlyVisibleElements: true, activateApp: false)
        let basename = "\(nowMillis())_\(safeComponent(key))_state"
        let screenshot = captureScreenshot(
            pid: pid_t(pid),
            basename: basename,
            clickPoint: clickPoint,
            fallbackFrame: windowFrame(from: traversal)
        )
        let bundle = runningApp?.bundleIdentifier ?? applicationBundleInfo(pid: pid_t(pid)).0
        let appName = runningApp?.localizedName ?? launchedAppName ?? app
        var elements = indexedElements(from: traversal, pid: pid, screenshot: screenshot)
        var ocrPayload: OCRPayload?
        var ocrError: String?
        if observationMode.includesOCR {
            if let screenshot {
                do {
                    let payload = try runOCR(
                        imagePath: screenshot.path,
                        screenshot: screenshot,
                        languages: ocrLanguages,
                        recognitionLevel: ocrRecognitionLevel
                    )
                    ocrPayload = payload
                    elements.append(contentsOf: ocrElements(from: payload))
                } catch {
                    ocrError = error.localizedDescription
                }
            } else {
                ocrError = "OCR requested, but screenshot capture failed."
            }
        }
        let visual = visualObservation(for: observationMode, screenshot: screenshot)
        let focused = elements.first { $0.focused == true }?.index

        var state = AppState(
            requestedApp: app,
            appName: appName,
            bundleIdentifier: bundle,
            pid: pid,
            windowTitle: windowTitle(from: traversal),
            screenshot: screenshot,
            observationMode: observationMode,
            elements: elements,
            focusedElementIndex: focused,
            ocrPayload: ocrPayload,
            ocrError: ocrError,
            visualObservation: visual,
            traversal: traversal,
            statePath: nil,
            textPath: nil,
            createdAt: Date().timeIntervalSince1970
        )
        try writeArtifacts(for: &state, basename: basename)
        states[key] = state
        if let bundle {
            states[appKey(bundle)] = state
        }
        states[appKey(appName)] = state
        return state
    }

    @MainActor
    func element(app: String, index: String, observationMode: ObservationMode = .axOCR) async throws -> (AppState, IndexedElement) {
        var state = try await state(for: app, observationMode: observationMode)
        if let element = state.elements.first(where: { $0.index == index }) {
            return (state, element)
        }
        state = try await self.state(for: app, forceRefresh: true, observationMode: observationMode)
        if let element = state.elements.first(where: { $0.index == index }) {
            return (state, element)
        }
        throw TactileMCPError.elementNotFound("Element index \(index) not found for \(app)")
    }

    @MainActor
    func relocateElement(
        app: String,
        original: IndexedElement,
        observationMode: ObservationMode = .ax
    ) async throws -> (AppState, IndexedElement) {
        let refreshed = try await state(for: app, forceRefresh: true, observationMode: observationMode)
        if let replacement = bestRelocatedElement(original: original, in: refreshed.elements) {
            return (refreshed, replacement)
        }
        throw TactileMCPError.elementNotFound("Element \(original.index) path became stale and could not be relocated by role/text/screenFrame")
    }

    @MainActor
    func refreshAfterAction(
        app: String,
        clickPoint: CGPoint? = nil,
        observationMode: ObservationMode? = nil
    ) async throws -> AppState {
        let previous = states[appKey(app)]
        let mode = observationMode ?? previous?.observationMode ?? .axOCR
        let languages = previous?.ocrPayload?.languages ?? ["zh-Hans", "en-US"]
        let recognitionLevel = previous?.ocrPayload?.recognitionLevel ?? "accurate"
        return try await state(
            for: app,
            forceRefresh: true,
            clickPoint: clickPoint,
            observationMode: mode,
            ocrLanguages: languages,
            ocrRecognitionLevel: recognitionLevel
        )
    }
}

let stateManager = StateManager()

func discoverApps() -> [AppRecord] {
    var records: [String: AppRecord] = [:]
    let frontmostPID = NSWorkspace.shared.frontmostApplication?.processIdentifier

    for app in NSWorkspace.shared.runningApplications {
        let name = app.localizedName ?? app.bundleIdentifier ?? "PID \(app.processIdentifier)"
        let key = app.bundleIdentifier ?? name
        records[key] = AppRecord(
            name: name,
            bundleIdentifier: app.bundleIdentifier,
            path: app.bundleURL?.path,
            pid: app.processIdentifier,
            running: true,
            frontmost: app.processIdentifier == frontmostPID
        )
    }

    let dirs = [
        "/Applications",
        "/System/Applications",
        "/System/Applications/Utilities",
        "\(NSHomeDirectory())/Applications",
    ]
    for dir in dirs {
        guard let items = try? FileManager.default.contentsOfDirectory(atPath: dir) else { continue }
        for item in items where item.hasSuffix(".app") {
            let path = "\(dir)/\(item)"
            guard let bundle = Bundle(path: path) else { continue }
            let name = bundle.localizedInfoDictionary?["CFBundleName"] as? String
                ?? bundle.infoDictionary?["CFBundleName"] as? String
                ?? String(item.dropLast(4))
            let id = bundle.bundleIdentifier
            let key = id ?? path
            if records[key] == nil {
                records[key] = AppRecord(name: name, bundleIdentifier: id, path: path, pid: nil, running: false, frontmost: false)
            }
        }
    }

    return records.values.sorted {
        if $0.frontmost != $1.frontmost { return $0.frontmost && !$1.frontmost }
        if $0.running != $1.running { return $0.running && !$1.running }
        return $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
    }
}

func listAppsSummary() throws -> String {
    let apps = discoverApps()
    var lines = ["Succeeded. Returned a text list of running/installed apps."]
    for app in apps.prefix(80) {
        var flags: [String] = []
        if app.frontmost { flags.append("frontmost") }
        if app.running { flags.append("running") }
        if let pid = app.pid { flags.append("pid=\(pid)") }
        let flagText = flags.isEmpty ? "" : " [\(flags.joined(separator: ", "))]"
        lines.append("- \(app.name) — \(app.bundleIdentifier ?? "no-bundle-id")\(flagText)")
    }
    let path = "\(outputRoot)/\(nowMillis())_list_apps.json"
    try writeText(try jsonString(apps), to: path)
    lines.append("json: \(path)")
    return lines.joined(separator: "\n")
}

func scrollDelta(direction: String, pages: Double) throws -> (Int32, Int32) {
    let amount = Int32(max(24, Int(abs(pages) * 600)))
    switch direction.lowercased() {
    case "down":
        return (-amount, 0)
    case "up":
        return (amount, 0)
    case "right":
        return (0, -amount)
    case "left":
        return (0, amount)
    default:
        throw TactileMCPError.invalidArgument("Unsupported scroll direction: \(direction)")
    }
}

func performCoordinateScroll(at point: CGPoint, deltaY: Int32, deltaX: Int32 = 0) throws {
    guard let source = CGEventSource(stateID: .hidSystemState) else {
        throw TactileMCPError.actionFailed("failed to create scroll event source")
    }

    CGWarpMouseCursorPosition(point)
    CGAssociateMouseAndMouseCursorPosition(boolean_t(1))
    let mouseMove = CGEvent(mouseEventSource: source, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)
    mouseMove?.post(tap: .cghidEventTap)
    usleep(20_000)

    let largest = max(abs(Int(deltaY)), abs(Int(deltaX)))
    let steps = max(1, min(24, Int(ceil(Double(largest) / 80.0))))
    var sentY = 0
    var sentX = 0
    for step in 1...steps {
        let targetY = Int((Double(deltaY) * Double(step) / Double(steps)).rounded())
        let targetX = Int((Double(deltaX) * Double(step) / Double(steps)).rounded())
        let chunkY = Int32(targetY - sentY)
        let chunkX = Int32(targetX - sentX)
        sentY = targetY
        sentX = targetX
        guard let event = CGEvent(
            scrollWheelEvent2Source: source,
            units: .pixel,
            wheelCount: 2,
            wheel1: chunkY,
            wheel2: chunkX,
            wheel3: 0
        ) else {
            throw TactileMCPError.actionFailed("failed to create scroll wheel event")
        }
        event.location = point
        event.post(tap: .cghidEventTap)
        usleep(16_000)
    }
}

func performSecondary(action: String, element: IndexedElement, state: AppState) throws {
    guard secondaryActionEnum.contains(action) else {
        throw TactileMCPError.invalidArgument("Unsupported secondary action \(action). Allowed: \(secondaryActionEnum.joined(separator: ", "))")
    }
    guard element.source == "ax" else {
        throw TactileMCPError.unsupportedAction("Element \(element.index) has source=\(element.source). OCR elements do not support secondary AX actions; use click, scroll, type_text, or raw screenshot coordinates instead.")
    }
    guard let path = element.axPath else {
        throw TactileMCPError.elementNotFound("Element \(element.index) has no axPath")
    }
    let ax = try axElement(pid: state.pid, path: path)
    switch action {
    case "Focus":
        try setAXBoolAttribute(ax, attribute: kAXFocusedAttribute as String, value: true)
    case "Select":
        try setAXBoolAttribute(ax, attribute: kAXSelectedAttribute as String, value: true)
    case "Deselect":
        try setAXBoolAttribute(ax, attribute: kAXSelectedAttribute as String, value: false)
    case "Press":
        try performAXAction(ax, action: kAXPressAction as String)
    case "Raise":
        try performAXAction(ax, action: "AXRaise")
    case "ShowMenu":
        try performAXAction(ax, action: "AXShowMenu")
    case "Confirm":
        try performAXAction(ax, action: "AXConfirm")
    case "Cancel":
        try performAXAction(ax, action: "AXCancel")
    case "Increment":
        try performAXAction(ax, action: "AXIncrement")
    case "Decrement":
        try performAXAction(ax, action: "AXDecrement")
    case "ScrollUp":
        try performAXAction(ax, action: "AXScrollUp")
    case "ScrollDown":
        try performAXAction(ax, action: "AXScrollDown")
    case "ScrollLeft":
        try performAXAction(ax, action: "AXScrollLeft")
    case "ScrollRight":
        try performAXAction(ax, action: "AXScrollRight")
    default:
        throw TactileMCPError.invalidArgument("Unsupported secondary action: \(action)")
    }
}

func performScrollFallbackForSecondaryAction(action: String, element: IndexedElement) throws -> Bool {
    guard action.hasPrefix("Scroll"), let center = try? elementCenter(element) else {
        return false
    }
    let direction = String(action.dropFirst("Scroll".count)).lowercased()
    let (dy, dx) = try scrollDelta(direction: direction, pages: 1)
    try performCoordinateScroll(at: center, deltaY: dy, deltaX: dx)
    return true
}

func toolContent(text: String, state: AppState? = nil) -> [Tool.Content] {
    var content: [Tool.Content] = [.text(text: text, annotations: nil, _meta: nil)]
    guard let state,
          state.observationMode.includesVisual,
          let screenshot = state.screenshot else {
        return content
    }
    do {
        let data = try Data(contentsOf: URL(fileURLWithPath: screenshot.path)).base64EncodedString()
        content.append(.image(data: data, mimeType: "image/png", annotations: nil, _meta: nil))
    } catch {
        fputs("warning: failed to attach screenshot image content: \(error.localizedDescription)\n", stderr)
    }
    return content
}

@MainActor
func handleToolCall(_ name: String, arguments: [String: Value]?) async throws -> [Tool.Content] {
    switch name {
    case "list_apps":
        return toolContent(text: try listAppsSummary())

    case "get_app_state":
        let app = try getRequiredString(from: arguments, key: "app")
        let mode = try parseObservationMode(try getOptionalString(from: arguments, key: "observation_mode"))
        let languages = parseOCRLanguages(try getOptionalString(from: arguments, key: "ocr_languages"))
        let recognitionLevel = try parseOCRRecognitionLevel(try getOptionalString(from: arguments, key: "ocr_recognition_level"))
        let summaryOptions = try parseSummaryOptions(from: arguments)
        let state = try await stateManager.state(
            for: app,
            forceRefresh: true,
            observationMode: mode,
            ocrLanguages: languages,
            ocrRecognitionLevel: recognitionLevel
        )
        return toolContent(text: compactStateSummary(state, prefix: "Succeeded. Returned app state and screenshot.", options: summaryOptions), state: state)

    case "click":
        let app = try getRequiredString(from: arguments, key: "app")
        if try getOptionalString(from: arguments, key: "element_index") != nil {
            throw MCPError.invalidParams("click only accepts coordinate inputs. Use x/y screenshot pixels or screen_x/screen_y macOS screen points. For AX elements, use perform_secondary_action instead.")
        }
        let state = try await stateManager.state(for: app)
        let point: CGPoint
        let screenX = try getOptionalDouble(from: arguments, key: "screen_x")
        let screenY = try getOptionalDouble(from: arguments, key: "screen_y")
        if screenX != nil || screenY != nil {
            guard let screenX, let screenY else {
                throw MCPError.invalidParams("click requires both screen_x and screen_y when either is supplied")
            }
            point = CGPoint(x: screenX, y: screenY)
        } else {
            let x = try getRequiredDouble(from: arguments, key: "x")
            let y = try getRequiredDouble(from: arguments, key: "y")
            let coordinateSpace = try parseCoordinateSpace(try getOptionalString(from: arguments, key: "coordinate_space"))
            if coordinateSpace == "screen" {
                point = CGPoint(x: x, y: y)
            } else {
                point = screenshotPointToScreen(x, y, state: state)
            }
        }
        let button = try getOptionalString(from: arguments, key: "mouse_button") ?? "left"
        let count = try getOptionalInt(from: arguments, key: "click_count") ?? 1
        NSRunningApplication(processIdentifier: pid_t(state.pid))?.activate(options: [])
        try? await Task.sleep(nanoseconds: 100_000_000)
        try performRawClick(at: point, button: button, count: count)
        let refreshed = try await stateManager.refreshAfterAction(app: app, clickPoint: point)
        return toolContent(text: compactStateSummary(refreshed, prefix: "Succeeded. Returned refreshed app state and screenshot."), state: refreshed)

    case "perform_secondary_action":
        let app = try getRequiredString(from: arguments, key: "app")
        let index = try getRequiredString(from: arguments, key: "element_index")
        let action = try getRequiredString(from: arguments, key: "action")
        if index.lowercased().hasPrefix("o") {
            throw TactileMCPError.unsupportedAction("Element \(index) has source=ocr. OCR elements do not support secondary AX actions; use click, scroll, type_text, or raw screenshot coordinates instead.")
        }
        let pair = try await stateManager.element(app: app, index: index, observationMode: .ax)
        guard pair.1.source == "ax" else {
            throw TactileMCPError.unsupportedAction("Element \(index) has source=\(pair.1.source). OCR elements do not support secondary AX actions; use click, scroll, type_text, or raw screenshot coordinates instead.")
        }
        var actionState = pair.0
        var actionElement = pair.1
        do {
            try performSecondary(action: action, element: actionElement, state: actionState)
        } catch {
            if isStaleAXPathError(error) {
                let relocated = try await stateManager.relocateElement(app: app, original: actionElement, observationMode: .ax)
                actionState = relocated.0
                actionElement = relocated.1
                do {
                    try performSecondary(action: action, element: actionElement, state: actionState)
                } catch {
                    let didFallback = try performScrollFallbackForSecondaryAction(action: action, element: actionElement)
                    if !didFallback {
                        throw error
                    }
                }
            } else {
                let didFallback = try performScrollFallbackForSecondaryAction(action: action, element: actionElement)
                if !didFallback {
                    throw error
                }
            }
        }
        let refreshed = try await stateManager.refreshAfterAction(app: app, observationMode: .ax)
        return toolContent(text: compactStateSummary(refreshed, prefix: "Succeeded. Returned refreshed app state."), state: refreshed)

    case "set_value":
        throw TactileMCPError.unsupportedAction("set_value is disabled. Use click or perform_secondary_action to focus an element, then use type_text or press_key for text input.")

    case "scroll":
        let app = try getRequiredString(from: arguments, key: "app")
        let index = try getOptionalString(from: arguments, key: "element_index")
        let direction = try getRequiredString(from: arguments, key: "direction")
        let pages = try getOptionalDouble(from: arguments, key: "pages") ?? 1
        let x = try getOptionalDouble(from: arguments, key: "x")
        let y = try getOptionalDouble(from: arguments, key: "y")

        var scrollPoint: CGPoint?
        if let index {
            let pair = try await stateManager.element(app: app, index: index)
            let element = pair.1
            if element.source == "ax" {
                do {
                    let action = "Scroll" + direction.prefix(1).uppercased() + direction.dropFirst().lowercased()
                    try performSecondary(action: action, element: element, state: pair.0)
                } catch {
                    let center = try elementCenter(element)
                    scrollPoint = center
                    let (dy, dx) = try scrollDelta(direction: direction, pages: pages)
                    try performCoordinateScroll(at: center, deltaY: dy, deltaX: dx)
                }
            } else {
                let center = try elementCenter(element)
                scrollPoint = center
                let (dy, dx) = try scrollDelta(direction: direction, pages: pages)
                try performCoordinateScroll(at: center, deltaY: dy, deltaX: dx)
            }
        } else {
            guard let x, let y else {
                throw TactileMCPError.invalidArgument("scroll requires either element_index or both x and y screenshot pixel coordinates")
            }
            let state = try await stateManager.state(for: app)
            let point = screenshotPointToScreen(x, y, state: state)
            scrollPoint = point
            let (dy, dx) = try scrollDelta(direction: direction, pages: pages)
            try performCoordinateScroll(at: point, deltaY: dy, deltaX: dx)
        }
        let refreshed = try await stateManager.refreshAfterAction(app: app, clickPoint: scrollPoint)
        return toolContent(text: compactStateSummary(refreshed, prefix: "Succeeded. Returned refreshed app state."), state: refreshed)

    case "drag":
        let app = try getRequiredString(from: arguments, key: "app")
        let state = try await stateManager.state(for: app)
        let start = screenshotPointToScreen(
            try getRequiredDouble(from: arguments, key: "from_x"),
            try getRequiredDouble(from: arguments, key: "from_y"),
            state: state
        )
        let end = screenshotPointToScreen(
            try getRequiredDouble(from: arguments, key: "to_x"),
            try getRequiredDouble(from: arguments, key: "to_y"),
            state: state
        )
        NSRunningApplication(processIdentifier: pid_t(state.pid))?.activate(options: [])
        try performDrag(from: start, to: end)
        let refreshed = try await stateManager.refreshAfterAction(app: app)
        return toolContent(text: compactStateSummary(refreshed, prefix: "Succeeded. Returned refreshed app state."), state: refreshed)

    case "press_key":
        let app = try getRequiredString(from: arguments, key: "app")
        let key = try getRequiredString(from: arguments, key: "key")
        let state = try await stateManager.state(for: app)
        NSRunningApplication(processIdentifier: pid_t(state.pid))?.activate(options: [])
        try? await Task.sleep(nanoseconds: 80_000_000)
        try pressKeyCombo(key)
        let refreshed = try await stateManager.refreshAfterAction(app: app)
        return toolContent(text: compactStateSummary(refreshed, prefix: "Succeeded. Returned refreshed app state."), state: refreshed)

    case "type_text":
        let app = try getRequiredString(from: arguments, key: "app")
        let text = try getRequiredString(from: arguments, key: "text")
        let state = try await stateManager.state(for: app)
        NSRunningApplication(processIdentifier: pid_t(state.pid))?.activate(options: [])
        do {
            try MacosUseSDK.writeText(text)
        } catch {
            try pasteTextWithClipboard(text)
        }
        let refreshed = try await stateManager.refreshAfterAction(app: app)
        return toolContent(text: compactStateSummary(refreshed, prefix: "Succeeded. Returned refreshed app state."), state: refreshed)

    default:
        throw MCPError.methodNotFound(name)
    }
}

func computerUseTools() -> [Tool] {
    let readOnly = Tool.Annotations(readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false)
    let mutating = Tool.Annotations(readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false)

    return [
        Tool(
            name: "list_apps",
            description: "List the apps on this computer. Returns running and installed apps with bundle identifiers and PIDs when available.",
            inputSchema: schema([:]),
            annotations: readOnly
        ),
        Tool(
            name: "get_app_state",
            description: "Start an app use session if needed, then get the state of the app's key window. observation_mode defaults to ax_ocr, returning Accessibility elements plus local OCRLine elements. The default summary is compact and keeps full state in /tmp/tactile-macos-mcp; use summary_mode=full or element_filter for more detail.",
            inputSchema: schema([
                "app": prop("string", "App name or bundle identifier"),
                "observation_mode": prop("string", "Observation mode. Defaults to ax_ocr. ax returns Accessibility elements only. ax_ocr also runs local macOS Vision OCR and appends OCRLine elements. ax_ocr_visual additionally attaches the screenshot image to the MCP result.", enumValues: observationModeEnum),
                "ocr_languages": prop("string", "Comma-separated macOS Vision OCR languages. Defaults to zh-Hans,en-US."),
                "ocr_recognition_level": prop("string", "macOS Vision OCR recognition level. Defaults to accurate.", enumValues: ocrRecognitionLevelEnum),
                "summary_mode": prop("string", "Summary verbosity. Defaults to compact. compact returns prioritized controls/text, full returns all matching elements, metadata omits element listings. Full untruncated state is always written to /tmp/tactile-macos-mcp.", enumValues: summaryModeEnum),
                "element_limit": prop("integer", "Maximum elements to include in the returned summary. Defaults to 80 for compact and unlimited for full."),
                "element_filter": prop("string", elementFilterDescription),
            ], required: ["app"]),
            annotations: readOnly
        ),
        Tool(
            name: "click",
            description: "Click coordinate-backed targets only. Use x/y for screenshot pixel coordinates, or screen_x/screen_y for macOS screen points. This tool does not accept AX element_index input; for AX element operations, use perform_secondary_action.",
            inputSchema: schema([
                "app": prop("string", "App name or bundle identifier"),
                "x": prop("number", "X coordinate. Defaults to screenshot pixel coordinates unless coordinate_space is screen."),
                "y": prop("number", "Y coordinate. Defaults to screenshot pixel coordinates unless coordinate_space is screen."),
                "coordinate_space": prop("string", "Coordinate space for x/y. Defaults to screenshot.", enumValues: coordinateSpaceEnum),
                "screen_x": prop("number", "X coordinate in macOS screen points. Overrides x/coordinate_space when paired with screen_y."),
                "screen_y": prop("number", "Y coordinate in macOS screen points. Overrides y/coordinate_space when paired with screen_x."),
                "mouse_button": prop("string", "Mouse button to click. Defaults to left.", enumValues: ["left", "right", "middle"]),
                "click_count": prop("integer", "Number of clicks. Defaults to 1"),
            ], required: ["app"]),
            annotations: mutating
        ),
        Tool(
            name: "perform_secondary_action",
            description: "Invoke a secondary accessibility action exposed by an element. Use this tool for AX element operations; click is coordinate-only. The action parameter is a fixed enum; unsupported element/action pairs return the element's actual supported AX actions.",
            inputSchema: schema([
                "app": prop("string", "App name or bundle identifier"),
                "element_index": prop("string", "Element identifier"),
                "action": prop("string", "Secondary accessibility action name", enumValues: secondaryActionEnum),
            ], required: ["app", "element_index", "action"]),
            annotations: mutating
        ),
        Tool(
            name: "set_value",
            description: "Disabled. This tool is kept in the schema for compatibility, but calls return an error. Use click or perform_secondary_action to focus an element, then type_text or press_key.",
            inputSchema: schema([
                "app": prop("string", "App name or bundle identifier"),
                "element_index": prop("string", "Element identifier"),
                "value": prop("string", "Value to assign"),
            ], required: ["app", "element_index", "value"]),
            annotations: mutating
        ),
        Tool(
            name: "scroll",
            description: "Scroll an element or screenshot pixel coordinate in a direction by a number of pages.",
            inputSchema: schema([
                "app": prop("string", "App name or bundle identifier"),
                "element_index": prop("string", "Element identifier"),
                "x": prop("number", "X coordinate in screenshot pixel coordinates"),
                "y": prop("number", "Y coordinate in screenshot pixel coordinates"),
                "direction": prop("string", "Scroll direction: up, down, left, or right"),
                "pages": prop("number", "Number of pages to scroll. Fractional values are supported. Defaults to 1"),
            ], required: ["app", "direction"]),
            annotations: mutating
        ),
        Tool(
            name: "drag",
            description: "Drag from one point to another using pixel coordinates from the latest screenshot.",
            inputSchema: schema([
                "app": prop("string", "App name or bundle identifier"),
                "from_x": prop("number", "Start X coordinate"),
                "from_y": prop("number", "Start Y coordinate"),
                "to_x": prop("number", "End X coordinate"),
                "to_y": prop("number", "End Y coordinate"),
            ], required: ["app", "from_x", "from_y", "to_x", "to_y"]),
            annotations: mutating
        ),
        Tool(
            name: "press_key",
            description: "Press a key or key-combination on the keyboard, including modifier and navigation keys. Supports examples like a, Return, Tab, super+c, cmd+a, Up, and KP_0.",
            inputSchema: schema([
                "app": prop("string", "App name or bundle identifier"),
                "key": prop("string", "Key or key combination to press"),
            ], required: ["app", "key"]),
            annotations: mutating
        ),
        Tool(
            name: "type_text",
            description: "Type literal text using keyboard input.",
            inputSchema: schema([
                "app": prop("string", "App name or bundle identifier"),
                "text": prop("string", "Literal text to type"),
            ], required: ["app", "text"]),
            annotations: mutating
        ),
    ]
}

func setupAndStartServer() async throws -> Server {
    let tools = computerUseTools()
    let server = Server(
        name: "tactile-macos-mcp",
        version: "0.1.0",
        instructions: """
        Computer Use style tools for macOS apps. Begin with get_app_state before action tools. For AX elements, use perform_secondary_action. click is coordinate-only and should be used for OCRLine or other visual/coordinate-backed targets, with OCRLine targets preferred over raw visual coordinates when both are available. get_app_state defaults to observation_mode=ax_ocr and summary_mode=compact, returning prioritized Accessibility/OCR elements while writing the full untruncated state and screenshots to /tmp/tactile-macos-mcp. Use element_filter to retrieve a small focused summary, summary_mode=metadata for paths only, summary_mode=full for the old full element listing, ax for AX-only speed/privacy, or ax_ocr_visual to also attach the screenshot for visual reasoning by the calling model. element_filter is a case-insensitive regex over element index/source/role/text/AX path/state/actions; use plain text for one target and regex OR like "search|搜索|输入|联系人|张仲岳" for multiple terms. element_filter only narrows get_app_state output and does not type into or search inside the app. If the expected target is missing, increase element_limit, use summary_mode=full, or inspect the full_element_dump path before taking action. Element output labels Accessibility coordinates as screenFrame/screenCenter and screenshot pixels as screenshotFrame/screenshotCenter. Raw click x/y defaults to screenshot pixel coordinates from the latest screenshot, but click also accepts coordinate_space=screen or screen_x/screen_y for macOS screen points. Scroll and drag raw coordinates remain screenshot pixels.
        """,
        capabilities: .init(tools: .init(listChanged: false))
    )

    await server.withMethodHandler(ListTools.self) { _ in
        ListTools.Result(tools: tools)
    }

    await server.withMethodHandler(ListResources.self) { _ in
        ListResources.Result(resources: [])
    }

    await server.withMethodHandler(ReadResource.self) { params in
        ReadResource.Result(contents: [.text("No resources are exposed by tactile-macos-mcp.", uri: params.uri)])
    }

    await server.withMethodHandler(ListPrompts.self) { _ in
        ListPrompts.Result(prompts: [])
    }

    await server.withMethodHandler(CallTool.self) { params in
        do {
            let content = try await handleToolCall(params.name, arguments: params.arguments)
            return CallTool.Result(content: content, isError: false)
        } catch let error as MCPError {
            return CallTool.Result(content: [.text(text: error.localizedDescription, annotations: nil, _meta: nil)], isError: true)
        } catch {
            return CallTool.Result(content: [.text(text: error.localizedDescription, annotations: nil, _meta: nil)], isError: true)
        }
    }

    try await server.start(transport: StdioTransport())
    return server
}

@main
struct TactileMacosMCP {
    static func main() async {
        do {
            let server = try await setupAndStartServer()
            await server.waitUntilCompleted()
        } catch {
            fputs("tactile-macos-mcp failed: \(error.localizedDescription)\n", stderr)
            exit(1)
        }
    }
}
