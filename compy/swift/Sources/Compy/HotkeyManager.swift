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

            // Read the extension's selection envelope.
            // The extension writes proactively on activation, editor change,
            // workspace-folder change, AND window-focus — so the envelope is
            // always current for the focused editor window. We only reject
            // workspaceRoot when the envelope is > 5 minutes old, which means
            // the extension isn't running and the file is from a prior session.
            var workspaceRoot: String? = nil
            var selectionFile: String? = nil
            var selectionLine: Int? = nil
            // Start with clipboard text as fallback — will be overridden by
            // extension's selectedText (the authoritative source).
            // Clipboard copy without a selection captures the whole line;
            // the extension accurately reports empty string when nothing is selected.
            var selectionText: String? = selectedText
            let maxStaleness: TimeInterval = 300  // 5 minutes — generous, rejects only abandoned sessions

            if let data = try? Data(contentsOf: URL(fileURLWithPath: self.selectionFile)),
               let envelope = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                // ts is seconds since epoch (written by the extension as Math.floor(Date.now()/1000)).
                let ts = (envelope["ts"] as? TimeInterval) ?? 0
                let now = Date().timeIntervalSince1970
                let isFresh = ts > 0 && (now - ts) < maxStaleness

                selectionFile = envelope["file"] as? String
                selectionLine = envelope["line"] as? Int
                // Always trust the extension's selectedText — it accurately reflects
                // the user's deliberate selection. Even an empty string (cursor with
                // no selection) correctly overrides the clipboard's whole-line copy.
                if let extText = envelope["selectedText"] as? String {
                    selectionText = extText
                }
                // Only trust workspaceRoot if the envelope is fresh.
                if isFresh {
                    workspaceRoot = envelope["workspaceRoot"] as? String
                    // Mark extension as connected so the overlay shows a green dot.
                    OverlayController.shared.state.extensionConnected = true
                }
            }

            // AX fallback: only use Accessibility API when the extension didn't
            // provide a workspaceRoot. The extension writes proactively on window
            // focus, editor change, and workspace-folder change — it is the
            // authoritative source. AX is unreliable on Electron editors (missing
            // document attributes) and can return paths from unrelated background
            // apps (Terminal, Finder), silently clobbering the correct root.
            //
            // The multi-window shared-file race (two editor windows competing
            // for /tmp/compy-selection.json) is a real but rare case. Its fix
            // (AX cross-check) caused more breakage than the race itself. When
            // the extension is connected, trust it.
            if workspaceRoot == nil,
               let axRoot = Self.resolveWorkspaceViaFrontmostEditor(),
               axRoot != "/" {
                Self._debugAXLog("AX-fallback: no extension root, using frontmost=\(axRoot)")
                workspaceRoot = axRoot
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

    // MARK: - Accessibility API Workspace Detection (zero-config fallback)

    /// Resolve the workspace via AX: first try the frontmost app, then scan
    /// ALL running apps for any editor with an open document.  Uses AXUIElement
    /// to read `kAXDocumentAttribute` off each app's focused window, then walks
    /// up to find the .git root.
    ///
    /// Called both as a fallback (no extension) AND as a cross-check on every
    /// hotkey (verify extension's workspaceRoot against the actual frontmost
    /// window — see onHotkey()).
    static func resolveWorkspaceViaFrontmostEditor() -> String? {
        // 1. Check COMPY_TARGET env var first — explicit override takes priority.
        if let target = ProcessInfo.processInfo.environment["COMPY_TARGET"],
           !target.isEmpty {
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: target, isDirectory: &isDir),
               isDir.boolValue {
                return target
            }
        }

        // 2. Try the FRONTMOST app first — the user's editor is almost always
        //    the app they're looking at when they press Cmd+Shift+Space.
        //    Previously we scanned all apps equally, which let Finder/Terminal
        //    win if they happened to appear earlier in the process list.
        let compyRoot = ProcessInfo.processInfo.environment["COMPY_ROOT"] ?? ""
        if let frontApp = NSWorkspace.shared.frontmostApplication {
            if let ws = Self._workspaceForApp(pid: frontApp.processIdentifier),
               ws != compyRoot {
                _debugAXLog("Accessibility: frontmost app pid=\(frontApp.processIdentifier) -> \(ws)")
                return ws
            }
        }

        // 3. Fall back to scanning all running apps.
        //    Budget only counts apps that actually returned a document path
        //    (apps without documents pass through instantly), so we don't
        //    exhaust the budget on Slack/browser/etc before reaching the editor.
        var bestFallback: String? = nil
        let runningApps = NSWorkspace.shared.runningApplications
        var scanned = 0
        let maxScan = 20  // cap on apps-with-documents checked

        for app in runningApps {
            guard scanned < maxScan else { break }
            guard app.activationPolicy == .regular else { continue }

            guard let workspace = Self._workspaceForApp(pid: app.processIdentifier) else {
                continue  // No document — don't count against budget
            }
            scanned += 1

            // Prefer any workspace that isn't the Compy project itself.
            // When Terminal is frontmost and CWD is Compy/, its document
            // attribute resolves to Compy/ — we skip that and keep looking.
            if !compyRoot.isEmpty && workspace == compyRoot {
                _debugAXLog("Accessibility: skipped Compy root from pid=\(app.processIdentifier)")
                bestFallback = workspace  // keep as last resort
                continue
            }

            _debugAXLog("Accessibility: found workspace pid=\(app.processIdentifier) -> \(workspace)")
            return workspace
        }

        // 4. No non-Comp workspace found — return the Compy fallback if we have one.
        _debugAXLog("Accessibility: returning fallback = \(bestFallback ?? "nil")")
        return bestFallback
    }

    /// Read an app's focused window document path and walk up to find the
    /// .git root.  Returns nil if the app has no focused window, no document,
    /// or the document path is invalid.
    private static func _workspaceForApp(pid: pid_t) -> String? {
        let appElement = AXUIElementCreateApplication(pid)

        var focusedWindow: CFTypeRef?
        let windowResult = AXUIElementCopyAttributeValue(
            appElement, kAXFocusedWindowAttribute as CFString, &focusedWindow
        )
        guard windowResult == .success,
              let fw = focusedWindow else { return nil }
        let windowElement = fw as! AXUIElement

        var docValue: CFTypeRef?
        let docResult = AXUIElementCopyAttributeValue(
            windowElement,
            kAXDocumentAttribute as CFString,
            &docValue
        )
        guard docResult == .success,
              let docURL = (docValue as? URL)
                  ?? (docValue as? String).flatMap({ URL(string: $0) })
                  ?? (docValue as? String).flatMap({ URL(fileURLWithPath: $0) }) else {
            return nil
        }

        let docPath = docURL.path
        // Walk up to find .git root.
        var current = URL(fileURLWithPath: docPath).deletingLastPathComponent()
        for _ in 0..<10 {
            let gitDir = current.appendingPathComponent(".git")
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: gitDir.path, isDirectory: &isDir) {
                _debugAXLog("Accessibility: workspace (git root) = \(current.path) (pid=\(pid))")
                return current.path
            }
            if current.path == "/" || current.pathComponents.count <= 1 { break }
            current = current.deletingLastPathComponent()
        }
        // No .git root — return document's parent directory.
        return URL(fileURLWithPath: docPath).deletingLastPathComponent().path
    }

    /// Write a diagnostic line to the debug log — non-essential, failures swallowed.
    private static func _debugAXLog(_ message: String) {
        let ts = ISO8601DateFormatter().string(from: Date())
        let line = "[AX \(ts)] \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        let url = URL(fileURLWithPath: "/tmp/compy-debug.log")
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: url, options: .atomic)
        }
    }
}
