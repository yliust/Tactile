// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "tactile-macos-mcp",
    platforms: [
        .macOS(.v13)
    ],
    dependencies: [
        .package(url: "https://github.com/modelcontextprotocol/swift-sdk.git", from: "0.11.0"),
        .package(path: "../../skills/tactile-macos/scripts/MacosUseSDK"),
    ],
    targets: [
        .executableTarget(
            name: "tactile-macos-mcp",
            dependencies: [
                .product(name: "MCP", package: "swift-sdk"),
                .product(name: "MacosUseSDK", package: "MacosUseSDK"),
            ],
            path: "Sources/MCPServer",
            swiftSettings: [.unsafeFlags(["-parse-as-library"])]
        ),
        .executableTarget(
            name: "screenshot-helper",
            path: "Sources/ScreenshotHelper"
        ),
    ]
)
