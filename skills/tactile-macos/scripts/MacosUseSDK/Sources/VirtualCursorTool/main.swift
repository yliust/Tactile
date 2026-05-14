import AppKit
import Foundation
import MacosUseSDK

@main
struct VirtualCursorTool {
    static func main() {
        guard CommandLine.arguments.count == 2 else {
            fputs("usage: VirtualCursorTool <state-json-path>\n", stderr)
            exit(2)
        }

        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let controller = VirtualCursorController(statePath: CommandLine.arguments[1])
        Task { @MainActor in
            controller.start()
        }
        app.run()
    }
}
