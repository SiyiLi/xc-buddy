// swift-tools-version: 5.9

import PackageDescription

let package = Package(
    name: "XCBuddy",
    platforms: [
        .macOS(.v12)
    ],
    products: [
        .executable(name: "XCBuddy", targets: ["XCBuddy"])
    ],
    dependencies: [
        .package(url: "https://github.com/sparkle-project/Sparkle", from: "2.6.0"),
        .package(url: "https://github.com/LebJe/TOMLKit.git", from: "0.6.0"),
    ],
    targets: [
        .executableTarget(
            name: "XCBuddy",
            dependencies: [
                .product(name: "Sparkle", package: "Sparkle"),
                .product(name: "TOMLKit", package: "TOMLKit"),
            ],
            path: "Sources/XCBuddy",
            exclude: ["Info.plist"],
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Sources/XCBuddy/Info.plist",
                ])
            ]
        )
    ]
)
