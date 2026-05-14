// swift-tools-version: 5.7
// The swift-tools-version declares the minimum version of Swift required to build this package.

import PackageDescription

let package = Package(
    name: "MacosUseSDK",
    platforms: [
        .macOS(.v12)
    ],
    products: [
        // Products define the executables and libraries a package produces, making them visible to other packages.
        .library(
            name: "MacosUseSDK",
            targets: ["MacosUseSDK"]),
        .executable(
            name: "TraversalTool",
            targets: ["TraversalTool"]),
        .executable(
            name: "HighlightTraversalTool",
            targets: ["HighlightTraversalTool"]),
        .executable(
            name: "InputControllerTool",
            targets: ["InputControllerTool"]),
        .executable(
            name: "VisualInputTool",
            targets: ["VisualInputTool"]),
        .executable(
            name: "VirtualCursorTool",
            targets: ["VirtualCursorTool"]),
        .executable(
            name: "AppOpenerTool",
            targets: ["AppOpenerTool"]),
        .executable(
            name: "ActionTool",
            targets: ["ActionTool"]),
        .executable(
            name: "TactileMacosTool",
            targets: ["TactileMacosTool"]),
    ],
    dependencies: [
        // Add any external package dependencies here later if needed
    ],
    targets: [
        // Targets are the basic building blocks of a package, defining a module or a test suite.
        // Targets can depend on other targets in this package and products from dependencies.
        .target(
            name: "MacosUseSDK",
            dependencies: [],
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("ApplicationServices"),
            ]
        ),
        .executableTarget(
            name: "TraversalTool",
            dependencies: ["MacosUseSDK"]
        ),
        .executableTarget(
            name: "HighlightTraversalTool",
            dependencies: [
                "MacosUseSDK",
            ]
        ),
        .executableTarget(
            name: "InputControllerTool",
            dependencies: ["MacosUseSDK"]
        ),
        .executableTarget(
            name: "VisualInputTool",
            dependencies: ["MacosUseSDK"]
        ),
        .executableTarget(
            name: "VirtualCursorTool",
            dependencies: ["MacosUseSDK"]
        ),
        .executableTarget(
            name: "AppOpenerTool",
            dependencies: ["MacosUseSDK"]
        ),
        .executableTarget(
            name: "ActionTool",
            dependencies: ["MacosUseSDK"]
        ),
        .executableTarget(
            name: "TactileMacosTool",
            dependencies: ["MacosUseSDK"]
        ),
        .testTarget(
            name: "MacosUseSDKTests",
            dependencies: ["MacosUseSDK"]
        ),
    ]
)
