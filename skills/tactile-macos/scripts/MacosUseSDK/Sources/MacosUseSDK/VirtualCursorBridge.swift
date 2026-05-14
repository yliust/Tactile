import CoreGraphics
import Foundation

private let virtualCursorStateFileName = "virtual-cursor.json"
private let virtualCursorPIDFileName = "virtual-cursor.pid"
private let virtualCursorBuildStampFileName = "virtual-cursor.buildstamp"
private let cursorClickPreEffectSeconds: TimeInterval = 0.08
private let cursorSettleWhenHiddenSeconds: TimeInterval = 0.72
private let cursorSettleMinSeconds: TimeInterval = 0.10
private let cursorSettleMaxSeconds: TimeInterval = 1.05
private let cursorMotionBaseSeconds: TimeInterval = 1.280 / 1.45

private func virtualCursorEnabled() -> Bool {
    let raw = ProcessInfo.processInfo.environment["TACTILE_VIRTUAL_CURSOR_ENABLED"] ?? ""
    return ["1", "true", "yes", "on"].contains(raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased())
}

private func packageRootURL() -> URL {
    let fileManager = FileManager.default

    if let raw = ProcessInfo.processInfo.environment["TACTILE_MACOS_SWIFT_PACKAGE"], raw.isEmpty == false {
        return URL(fileURLWithPath: raw).standardizedFileURL
    }

    let cwd = URL(fileURLWithPath: fileManager.currentDirectoryPath).standardizedFileURL
    if fileManager.fileExists(atPath: cwd.appendingPathComponent("Package.swift").path) {
        return cwd
    }

    if var current = Bundle.main.executableURL?.deletingLastPathComponent().standardizedFileURL {
        for _ in 0..<8 {
            if fileManager.fileExists(atPath: current.appendingPathComponent("Package.swift").path) {
                return current
            }
            let parent = current.deletingLastPathComponent()
            if parent.path == current.path {
                break
            }
            current = parent
        }
    }

    return cwd
}

private func virtualCursorStateDirectoryURL() -> URL {
    packageRootURL().appendingPathComponent(".state", isDirectory: true)
}

private func virtualCursorStateURL() -> URL {
    virtualCursorStateDirectoryURL().appendingPathComponent(virtualCursorStateFileName)
}

private func virtualCursorPIDURL() -> URL {
    virtualCursorStateDirectoryURL().appendingPathComponent(virtualCursorPIDFileName)
}

private func virtualCursorBuildStampURL() -> URL {
    virtualCursorStateDirectoryURL().appendingPathComponent(virtualCursorBuildStampFileName)
}

private func virtualCursorToolURL() -> URL? {
    guard let executableURL = Bundle.main.executableURL else { return nil }
    let candidate = executableURL.deletingLastPathComponent().appendingPathComponent("VirtualCursorTool")
    guard FileManager.default.isExecutableFile(atPath: candidate.path) else {
        return nil
    }
    return candidate
}

private func ensureVirtualCursorStateDirectory() throws {
    try FileManager.default.createDirectory(
        at: virtualCursorStateDirectoryURL(),
        withIntermediateDirectories: true
    )
}

private func writeText(_ value: String, to url: URL) throws {
    try ensureVirtualCursorStateDirectory()
    try value.write(to: url, atomically: true, encoding: .utf8)
}

private func readText(from url: URL) -> String? {
    try? String(contentsOf: url, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
}

private func readCursorPID() -> Int32? {
    guard let raw = readText(from: virtualCursorPIDURL()), let pid = Int32(raw) else {
        return nil
    }
    return pid
}

private func processIsAlive(_ pid: Int32) -> Bool {
    kill(pid, 0) == 0
}

private func stopProcess(_ pid: Int32) {
    _ = kill(pid, SIGTERM)
}

private func virtualCursorBuildStamp(for url: URL) -> String {
    let values = try? url.resourceValues(forKeys: [.contentModificationDateKey, .fileSizeKey])
    let modified = values?.contentModificationDate?.timeIntervalSince1970 ?? 0
    let size = values?.fileSize ?? 0
    return "\(Int64(modified * 1000))-\(size)"
}

private func writeCursorState(
    point: CGPoint?,
    event: String = "idle",
    label: String? = nil,
    visible: Bool = true
) throws {
    try ensureVirtualCursorStateDirectory()
    let cursorPoint = VirtualCursorPoint(
        x: Double(point?.x ?? 0),
        y: Double(point?.y ?? 0)
    )
    let state = VirtualCursorState(
        visible: visible,
        point: cursorPoint,
        event: event,
        label: label,
        updatedAt: Date().timeIntervalSince1970
    )
    let data = try JSONEncoder().encode(state)
    let url = virtualCursorStateURL()
    let tmp = url.appendingPathExtension("tmp")
    try data.write(to: tmp, options: .atomic)
    if FileManager.default.fileExists(atPath: url.path) {
        _ = try? FileManager.default.removeItem(at: url)
    }
    try FileManager.default.moveItem(at: tmp, to: url)
}

private func readCursorState() -> VirtualCursorState? {
    guard let data = try? Data(contentsOf: virtualCursorStateURL()) else {
        return nil
    }
    return try? JSONDecoder().decode(VirtualCursorState.self, from: data)
}

private func ensureVirtualCursorProcess() -> Bool {
    guard let toolURL = virtualCursorToolURL() else {
        fputs("warning: VirtualCursorTool is not available next to \(Bundle.main.executableURL?.path ?? "current executable"); skipping virtual cursor.\n", stderr)
        return false
    }

    let stamp = virtualCursorBuildStamp(for: toolURL)
    if let pid = readCursorPID(),
       processIsAlive(pid),
       readText(from: virtualCursorBuildStampURL()) == stamp {
        return true
    }

    if let pid = readCursorPID(), processIsAlive(pid) {
        stopProcess(pid)
        Thread.sleep(forTimeInterval: 0.05)
    }

    do {
        try ensureVirtualCursorStateDirectory()
        if FileManager.default.fileExists(atPath: virtualCursorStateURL().path) == false {
            try writeCursorState(point: nil, event: "idle", visible: false)
        }

        let process = Process()
        process.executableURL = toolURL
        process.arguments = [virtualCursorStateURL().path]
        process.currentDirectoryURL = packageRootURL()
        if let null = FileHandle(forWritingAtPath: "/dev/null") {
            process.standardOutput = null
            process.standardError = null
        }
        try process.run()
        try writeText("\(process.processIdentifier)\n", to: virtualCursorPIDURL())
        try writeText("\(stamp)\n", to: virtualCursorBuildStampURL())
        return true
    } catch {
        fputs("warning: failed to start VirtualCursorTool: \(error.localizedDescription)\n", stderr)
        return false
    }
}

private func lastVisibleCursorPoint() -> CGPoint? {
    guard let state = readCursorState(), state.visible else {
        return nil
    }
    return CGPoint(x: state.point.x, y: state.point.y)
}

private func cursorSettleDelay(to point: CGPoint) -> TimeInterval {
    guard let previous = lastVisibleCursorPoint() else {
        return cursorSettleWhenHiddenSeconds
    }

    let distance = hypot(point.x - previous.x, point.y - previous.y)
    if distance < 2 {
        return cursorSettleMinSeconds
    }

    let factor = max(0.55, min(1.80, Double(distance / 520.0)))
    let delay = max(0.42, cursorMotionBaseSeconds * factor)
    return min(max(delay, cursorSettleMinSeconds), cursorSettleMaxSeconds)
}

internal func prepareVirtualCursorAXPress(at point: CGPoint) {
    guard virtualCursorEnabled() else {
        return
    }
    guard ensureVirtualCursorProcess() else {
        return
    }

    do {
        let delay = cursorSettleDelay(to: point)
        try writeCursorState(point: point, event: "move", visible: true)
        Thread.sleep(forTimeInterval: delay)
        try writeCursorState(point: point, event: "click", visible: true)
        Thread.sleep(forTimeInterval: cursorClickPreEffectSeconds)
    } catch {
        fputs("warning: failed to update virtual cursor: \(error.localizedDescription)\n", stderr)
    }
}
