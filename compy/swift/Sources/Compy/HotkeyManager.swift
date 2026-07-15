// HotkeyManager.swift — Compy macOS global hotkey
//
// Registers Cmd+Shift+Space as the global Compy trigger. When fired, it:
//   1. Saves the current clipboard.
//   2. Synthesizes Cmd+C (clipboard-swap fallback per spec §4).
//   3. Reads the selection JSON file from the companion extension (if present).
//   4. Opens the overlay with selection context pre-filled.
//
// The companion extension (compy/extension/) writes to /tmp/compy-selection.json
// on the same hotkey — we read it after a short delay to let the FS sync.

import AppKit
import Carbon.HIToolbox.Events

final class HotkeyManager {
    static let shared = HotkeyManager()
    private var monitor: Any?

    private let selectionFile = "/tmp/compy-selection.json"

    func register() {
        monitor = NSEvent.addGlobalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
            guard let self = self else { return }
            guard event.modifierFlags.contains([.command, .shift]) else { return }
            guard event.keyCode == UInt16(kVK_Space) else { return }
            self.onHotkey()
        }
    }

    private func onHotkey() {
        let pasteboard = NSPasteboard.general
        let oldClipboard = pasteboard.string(forType: .string)

        // Clipboard-swap: synthesize Cmd+C to capture the user's selection.
        let src = CGEventSource(stateID: .hidSystemState)
        let down = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: true)
        let up = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: false)
        down?.flags = .maskCommand
        up?.flags = .maskCommand
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)

        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(80)) {
            let selectedText = pasteboard.string(forType: .string)

            // Restore original clipboard.
            if let old = oldClipboard {
                pasteboard.clearContents()
                pasteboard.setString(old, forType: .string)
            }

            // Read the extension's selection envelope, but reject stale entries.
            // The extension writes proactively on workspace/editor changes, so a
            // fresh envelope confirms the real active workspace. If the JSON is
            // > 5 seconds old, the extension didn't fire (editor out of focus) —
            // discard workspaceRoot and let resolveActiveWorkspace take over.
            var workspaceRoot: String? = nil
            var selectionFile: String? = nil
            var selectionLine: Int? = nil
            var selectionText: String? = selectedText
            let maxStaleness: TimeInterval = 5  // seconds

            if let data = try? Data(contentsOf: URL(fileURLWithPath: self.selectionFile)),
               let envelope = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                // ts is seconds since epoch (written by the extension as Math.floor(Date.now()/1000)).
                let ts = (envelope["ts"] as? TimeInterval) ?? 0
                let now = Date().timeIntervalSince1970
                let isFresh = ts > 0 && (now - ts) < maxStaleness

                selectionFile = envelope["file"] as? String
                selectionLine = envelope["line"] as? Int
                if let extText = envelope["selectedText"] as? String, !extText.isEmpty {
                    selectionText = extText
                }
                // Only trust workspaceRoot if the envelope is fresh.
                if isFresh {
                    workspaceRoot = envelope["workspaceRoot"] as? String
                }
            }

            OverlayController.shared.toggle(
                selectedText: selectionText,
                file: selectionFile,
                line: selectionLine,
                workspaceRoot: workspaceRoot
            )
        }
    }

    deinit {
        if let monitor = monitor { NSEvent.removeMonitor(monitor) }
    }
}
