// OverlayPanel.swift — Compy macOS overlay (Scaffold)
//
// Compiles on the user's machine with Xcode. This is REFERENCE ONLY — it is not built in
// the test pipeline. Per `.agent/code-search-jarvis-spec.md` §5a, this is the native
// SwiftUI app that hosts the floating always-on-top window.
//
// Build prerequisites on the user's machine (NOT this dev env):
//   - Xcode 15+ with Swift 5.9+
//   - macOS 13+ deployment target
//   - whisper.cpp + mlx-swift (per spec §5a — wiring is out of scope here)
//
// What this file does:
//   - Defines `CompyPanel: NSPanel` with `.nonactivatingPanel` style mask so it doesn't
//     steal focus from the user's editor when the overlay opens.
//   - Pins to top-right via `.topRight` alignment rect origin.
//   - Owns an `OverlayState` ObservableObject that drives the §3 state machine.
//
// Production work (not in this scaffold):
//   - Wire OverlayState's `submit()` call to the compy daemon's Unix socket
//     (`/tmp/compy-selection.sock`) and a separate result-pipe socket.
//   - Replace the placeholder `TextField` body with the §3 mic/text-mode toggle, the
//     result-list view, and the §3a degraded-result/no-match empty states.
//   - Hook the compy hotkey (Cmd+Shift+Space — see HotkeyManager.swift).

import SwiftUI
import AppKit

@main
struct CompyApp: App {
    @NSApplicationDelegateAdaptor(CompAppDelegate.self) private var appDelegate
    var body: some Scene { Settings { EmptyView() } }
}

final class CompAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching() {
        NSApp.setActivationPolicy(.accessory)  // no Dock icon
        HotkeyManager.shared.register()
        OverlayController.shared.show()
    }
}

final class OverlayController {
    static let shared = OverlayController()
    private var panel: CompyPanel?
    private let state = OverlayState()

    func show() {
        if panel != nil { return }
        let panel = CompyPanel(
            contentRect: NSRect(x: 0, y: 0, width: 580, height: 320),
            styleMask: [.titled, .closable, .resizable, .nonactivatingPanel, .fullSizeContentView],
            backing: .buffered, defer: true,
        )
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        panel.contentView = NSHostingView(rootView: OverlayView().environmentObject(state))
        alignTopRight(panel)
        panel.orderFrontRegardless()
        self.panel = panel
    }

    private func alignTopRight(_ panel: CompyPanel) {
        guard let screen = NSScreen.main else { return }
        let screenFrame = screen.visibleFrame
        let panelSize = panel.frame.size
        let xOrigin = screenFrame.maxX - panelSize.width - 16
        let yOrigin = screenFrame.maxY - panelSize.height - 16
        panel.setFrameOrigin(NSPoint(x: xOrigin, y: yOrigin))
    }
}

final class CompyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

final class OverlayState: ObservableObject {
    enum Mode { case mic, text }
    enum Phase { case empty, typeOrSpeak, processing, results, noMatch, degraded }
    @Published var mode: Mode = .mic
    @Published var phase: Phase = .empty
    @Published var text: String = ""
    @Published var results: [RankedHit] = []
}
