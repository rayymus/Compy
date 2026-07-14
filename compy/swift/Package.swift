// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Compy",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Compy", targets: ["Compy"])
    ],
    targets: [
        .executableTarget(
            name: "Compy",
            path: "Sources/Compy",
            resources: []
        )
    ]
)
