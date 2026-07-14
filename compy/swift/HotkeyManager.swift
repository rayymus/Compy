// HotkeyManager.swift — Compy macOS global hotkey
//
// Registers Cmd+Shift+Space as the global Compy-trigger. Uses two monitors:
//   1. A local monitor (NSEvent.addLocalMonitorForEvents) — works without permissions
//      when the overlay is the active window.
//   2. A global monitor (NSEvent.addGlobalMonitorForEvents) — catches the hotkey from
//      any app, but requires Accessibility permission in System Settings.
//
// On hotkey:
//   1. Saves the current clipboard (spec §4 clipboard-swap fallback).
//   2. Synthesizes Cmd+C to capture the user's selection.
//   3. Tells the overlay to appear (OverlayController.shared.show()).
//
// Also reads the companion extension's JSON file (/tmp/compy-selection.json) if present
// — this provides file/line/workspace grounding that clipboard-swap can't.

import AppKit
import Carbon.HIToolbox.Events

final class HotkeyManager {
    static let shared = HotkeyManager()
    private var globalMonitor: Any?
    private var localMonitor: Any?
    private var savedClipboard: String?
    private var accessibilityWarned = false

    func register() {
        // Local monitor — works without permissions, catches hotkey when overlay is focused.
        localMonitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
            guard let self = self else { return event }
            if self.isCompyHotkey(event) {
                self.onHotkey()
                return nil  // consume the event
            }
            return event
        }

        // Global monitor — requires Accessibility permission. Works from any app.
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.keyDown]) { [weak self] event in
            guard let self = self else { return }
            if self.isCompyHotkey(event) {
                self.onHotkey()
            }
        }

        // Check if global monitor is actually working (indirectly).
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.checkAccessibility()
        }
    }

    private func isCompyHotkey(_ event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard flags == [.command, .shift] else { return false }
        guard event.keyCode == UInt16(kVK_Space) else { return false }
        return true
    }

    private func checkAccessibility() {
        // AXIsProcessTrusted is the canonical check, but requires the app to be signed
        // with a hardened runtime entitlement. Instead, we use a heuristic: if the global
        // monitor was registered but we detect the app isn't trusted, show a warning.
        // The actual check: launch a system-wide CGEvent post and see if we can read it.
        // For now, we show the warning at launch since most users won't have granted it yet.
        let trusted = AXIsProcessTrusted()
        if !trusted && !accessibilityWarned {
            accessibilityWarned = true
            DispatchQueue.main.async {
                let alert = NSAlert()
                alert.messageText = "Accessibility Permission Required"
                alert.informativeText = """
                    Compy needs Accessibility permission to detect Cmd+Shift+Space from other apps.

                    Open System Settings → Privacy & Security → Accessibility,
                    then add and enable Compy.

                    Without this, the hotkey only works when Compy is the active window.
                    """
                alert.alertStyle = .warning
                alert.addButton(withTitle: "Open System Settings")
                alert.addButton(withTitle: "Later")
                if alert.runModal() == .alertFirstButtonReturn {
                    NSWorkspace.shared.open(
                        URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!
                    )
                }
            }
        }
    }

    private func onHotkey() {
        let pasteboard = NSPasteboard.general
        savedClipboard = pasteboard.string(forType: .string)
        // Synthesize Cmd+C — the pasteboard now reflects the user's selection.
        let src = CGEventSource(stateID: .hidSystemState)
        let down = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: true)  // 'c'
        let up = CGEvent(keyboardEventSource: src, virtualKey: 0x08, keyDown: false)
        down?.flags = .maskCommand
        up?.flags = .maskCommand
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
        // Defer overlay open briefly so the synthesized Cmd+C completes.
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(60)) {
            OverlayController.shared.show()
            // pasteboard.string(forType: .string) is now the new clipboard; the controller
            // reads it as the fallback selection.
        }
    }

    deinit {
        if let monitor = globalMonitor { NSEvent.removeMonitor(monitor) }
        if let monitor = localMonitor { NSEvent.removeMonitor(monitor) }
    }
}
