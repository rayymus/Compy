// OverlayPanel.swift — Compy macOS overlay
//
// Native SwiftUI overlay: NSPanel, top-right, always-on-top, Cmd+Shift+Space trigger.
// Implements the full §3 state machine: mic/text modes, query submission via daemon
// subprocess, results display with degraded/no-match states, click-to-open in editor,
// Esc/click-outside dismiss, and reasoner-source badge.

import SwiftUI
import AppKit
import Carbon.HIToolbox.Events
import Speech
import AVFoundation

// MARK: - Editor Opener

/// Resolves the best available editor CLI to open a file at a given line.
///
/// `basePath` must be set once at launch to the project root so that relative
/// file paths from the daemon (e.g. `compy/daemon/orchestrator.py`) are resolved
/// to absolute paths before being passed to `agy-ide -g`.
struct EditorOpener {
    /// Authoritative project root — MUST be set by CompAppDelegate on launch.
    /// Defaults to "/" so uninitialized usage doesn't silently create files
    /// in a user directory; relative paths resolve to root instead.
    static var basePath: String = "/"

    /// Keep a reference to the editor subprocess so it isn't deallocated
    /// before the goto command completes. Serial queue protects against
    /// concurrent mutations from the background dispatch and terminationHandler
    /// (which fires on an arbitrary queue per Apple docs).
    private static var pendingEditors: [Process] = []
    private static let pendingLock = NSLock()

    /// Ordered candidates: agy-ide (Antigravity 1.x), cursor, code.
    /// Each is tried in order; first one that exists and launches cleanly wins.
    /// Final fallback: NSWorkspace.open (file only, no line jump).
    ///
    /// Runs the editor CLI on a background queue so the overlay UI stays
    /// responsive and no temporary editor window flashes on screen.
    static func open(file: String, line: Int) {
        // Resolve relative paths against the project root so the editor CLI
        // always receives an absolute path regardless of its current workspace.
        let resolved: String
        if file.hasPrefix("/") {
            resolved = file
        } else {
            resolved = URL(fileURLWithPath: file,
                           relativeTo: URL(fileURLWithPath: basePath)).path
        }

        let home = NSHomeDirectory()
        let candidates = [
            "\(home)/.antigravity-ide/antigravity-ide/bin/agy-ide",
            "/usr/local/bin/cursor",
            "/usr/local/bin/code",
        ]
        DispatchQueue.global(qos: .userInitiated).async {
            for path in candidates {
                guard FileManager.default.isExecutableFile(atPath: path) else { continue }
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: path)
                proc.arguments = ["-g", "\(resolved):\(line)"]
                do {
                    pendingLock.lock()
                    pendingEditors.append(proc)
                    pendingLock.unlock()
                    try proc.run()
                    proc.terminationHandler = { [weak proc] _ in
                        guard let proc = proc else { return }
                        pendingLock.lock()
                        pendingEditors.removeAll { $0 === proc }
                        pendingLock.unlock()
                    }
                    return  // first match wins
                } catch {
                    pendingLock.lock()
                    pendingEditors.removeAll { $0 === proc }
                    pendingLock.unlock()
                    continue
                }
            }
            // Fallback: no editor CLI found — open the file in the default app.
            DispatchQueue.main.async {
                NSWorkspace.shared.open(URL(fileURLWithPath: resolved))
            }
        }
    }
}

// MARK: - App Entry Point

@main
struct CompyApp: App {
    @NSApplicationDelegateAdaptor(CompAppDelegate.self) private var appDelegate
    var body: some Scene { Settings { EmptyView() } }
}

// MARK: - App Delegate

final class CompAppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        // Set the editor opener's base path early so result-click file paths
        // are always resolved against the authoritative project root.
        EditorOpener.basePath = OverlayView.resolveRepoRoot().path
        HotkeyManager.shared.register()
    }
}

// MARK: - Overlay Controller

final class OverlayController: NSObject, NSWindowDelegate {
    static let shared = OverlayController()
    private var panel: CompyPanel?
    private let state = OverlayState()

    func show(
        selectedText: String? = nil,
        file: String? = nil,
        line: Int? = nil,
        workspaceRoot: String? = nil
    ) {
        // Always reset transient state so the overlay starts clean.
        // (Previously only reset when panel != nil, which leaked stale phase like
        // .results across dismiss/re-open cycles, breaking compact sizing.)
        state.reset()

        state.selectionText = selectedText ?? ""
        state.selectionFile = file
        state.selectionLine = line
        state.workspaceRoot = workspaceRoot

        if let t = selectedText, !t.isEmpty, state.text.isEmpty {
            state.text = t
        }

        // If already visible, just update state and return.
        guard panel == nil else { return }

        // Start compact (input-bar only) when no results; expand on first query.
        let initialHeight: CGFloat = (state.phase == .empty) ? 72 : 420
        // No .nonactivatingPanel — the overlay needs keyboard focus so the
        // text field is immediately typeable when the hotkey fires.
        let panel = CompyPanel(
            contentRect: NSRect(x: 0, y: 0, width: 600, height: initialHeight),
            styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: true
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
        panel.isReleasedWhenClosed = false
        panel.delegate = self  // click-outside → dismiss
        alignTopRight(panel)
        panel.makeKeyAndOrderFront(nil)
        // Briefly activate the app so the panel gets keyboard focus.
        // .accessory policy means no Dock icon — focus returns to the
        // previous app when the user clicks outside (windowDidResignKey).
        NSApp.activate(ignoringOtherApps: true)
        self.panel = panel
    }

    func hide() {
        guard panel != nil else { return }  // idempotent — safe against double-fire from Esc + windowDidResignKey
        panel?.close()
        panel = nil
        // Hand focus back to the previously active app now that our only
        // window is gone. Compy runs as .accessory (no Dock icon) so there's
        // nothing for the user to see.
        NSApp.deactivate()
    }

    func toggle(
        selectedText: String? = nil,
        file: String? = nil,
        line: Int? = nil,
        workspaceRoot: String? = nil
    ) {
        if panel != nil {
            hide()
        } else {
            show(selectedText: selectedText, file: file, line: line, workspaceRoot: workspaceRoot)
        }
    }

    // MARK: NSWindowDelegate — click-outside dismiss

    func windowDidResignKey(_ notification: Notification) {
        hide()
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

// MARK: - NSPanel subclass

final class CompyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

// MARK: - Observable State

/// Mirrors the STT wrapper's JSON output.
struct STTResult: Codable {
    let text: String
    let success: Bool
    let error: String?
}

final class OverlayState: ObservableObject {
    enum Mode { case mic, text }
    enum Phase { case empty, processing, results, noMatch, degraded }

    @Published var mode: Mode = .mic
    @Published var phase: Phase = .empty
    @Published var text: String = ""
    @Published var results: [RankedHit] = []
    @Published var reasonText: String? = nil
    @Published var isRecording: Bool = false
    @Published var sttError: String? = nil

    var selectionText: String = ""
    var selectionFile: String? = nil
    var selectionLine: Int? = nil
    var workspaceRoot: String? = nil

    // Speech recognition state — must live on a class (ObservableObject) since
    // AVAudioEngine callbacks need stable references.
    let speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))!
    let audioEngine = AVAudioEngine()
    var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    var recognitionTask: SFSpeechRecognitionTask?

    /// Dominant source across all hits (e.g. "freebuff", "heuristic", "grep").
    var sourceLabel: String {
        guard let first = results.first else { return "" }
        let sources = Dictionary(grouping: results, by: \.source)
            .mapValues { $0.count }
            .sorted { $0.value > $1.value }
        return sources.first?.key ?? first.source
    }

    /// Accumulated finalized STT text — preserved across recognition-restart loops.
    var recognizedTextPrefix: String = ""

    /// Reset transient UI state (preserve selection context).
    func reset() {
        mode = .mic
        phase = .empty
        text = ""
        recognizedTextPrefix = ""
        results = []
        reasonText = nil
        isRecording = false
        sttError = nil
    }

    /// Stop any active speech recognition.
    func stopRecording() {
        audioEngine.stop()
        audioEngine.inputNode.removeTap(onBus: 0)
        recognitionRequest?.endAudio()
        recognitionRequest = nil
        recognitionTask?.cancel()
        recognitionTask = nil
        isRecording = false
        mode = .text
    }
}

// MARK: - Overlay View

struct OverlayView: View {
    @EnvironmentObject var state: OverlayState
    @FocusState private var isFocused: Bool
    @State private var escMonitor: Any? = nil

    /// Only show the content area when there's something to display —
    /// keep the overlay compact (just the input bar) until a query is submitted.
    private var shouldShowContent: Bool {
        if state.phase == .empty { return false }
        return true
    }

    var body: some View {
        VStack(spacing: 0) {
            inputBar
            if shouldShowContent {
                Divider()
                contentArea
            }
        }
        .frame(width: 600)
        .frame(minHeight: 72)
        .background(Color(NSColor.windowBackgroundColor))
        .onChange(of: state.phase) { _, newPhase in
            guard let window = NSApp.windows.first(where: { $0 is CompyPanel }) else { return }
            let targetHeight: CGFloat = (newPhase == .empty) ? 72 : 420
            var frame = window.frame
            if abs(frame.size.height - targetHeight) > 1 {
                frame.origin.y += (frame.size.height - targetHeight)
                frame.size.height = targetHeight
                window.setFrame(frame, display: true, animate: true)
            }
        }
        .onTapGesture {
            guard state.phase != .results, state.phase != .degraded else { return }
            guard !state.isRecording else { return }
            isFocused = false
            state.mode = .mic
        }
        .onAppear {
            escMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
                if event.keyCode == UInt16(kVK_Escape) {
                    OverlayController.shared.hide()
                    return nil
                }
                return event
            }
        }
        .onDisappear {
            if let monitor = escMonitor {
                NSEvent.removeMonitor(monitor)
                escMonitor = nil
            }
        }
    }

    // MARK: - Input Bar

    private var inputBar: some View {
        HStack(spacing: 12) {
            // Mic button — triggers STT recording or switches to text mode.
            micButton

            if state.mode == .mic && !isFocused && state.text.isEmpty && !state.isRecording {
                Text("Speak a query or click to type...")
                    .font(.system(size: 18))
                    .foregroundColor(Color(NSColor.placeholderTextColor))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                    .onTapGesture {
                        state.mode = .text
                        isFocused = true
                    }
            } else if state.isRecording {
                recordingIndicator
            } else {
                TextField("Search codebase...", text: $state.text)
                    .textFieldStyle(.plain)
                    .font(.system(size: 18))
                    .focused($isFocused)
                    .onSubmit { submitQuery() }
                    .onChange(of: isFocused) { _, focused in
                        if focused { state.mode = .text }
                    }
            }

            if !state.text.isEmpty {
                Button(action: {
                    withAnimation(.easeOut(duration: 0.15)) {
                        state.text = ""
                        state.phase = .empty
                        state.results = []
                    }
                }) {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 14))
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 16)
        .background(Color(NSColor.controlBackgroundColor))
    }

    // MARK: - Mic Button (STT)

    private var micButton: some View {
        Button(action: {
            if state.isRecording {
                stopRecording()
            } else if state.mode == .mic {
                startRecording()
            } else {
                state.mode = .mic
                isFocused = false
            }
        }) {
            Image(systemName: micButtonIcon)
                .font(.system(size: 16, weight: .medium))
                .foregroundColor(micButtonColor)
                .frame(width: 22)
        }
        .buttonStyle(.plain)
        .help(state.isRecording ? "Click to stop recording" : (state.mode == .mic ? "Click to start recording" : "Switch to mic mode"))
    }

    private var micButtonIcon: String {
        if state.isRecording { return "mic.fill" }
        if state.sttError != nil { return "mic.slash.fill" }
        return state.mode == .mic ? "mic.fill" : "mic"
    }

    private var micButtonColor: Color {
        if state.isRecording { return .red }
        if state.sttError != nil { return .orange }
        return isFocused ? .accentColor : .secondary
    }

    // MARK: - Recording Indicator (shows live transcription)

    private var recordingIndicator: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(Color.red)
                .frame(width: 8, height: 8)
                .scaleEffect(1.3)
                .animation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true), value: state.isRecording)
            if state.text.isEmpty {
                Text("Listening...")
                    .font(.system(size: 18, weight: .medium))
                    .foregroundColor(.secondary)
            } else {
                Text(state.text)
                    .font(.system(size: 18))
                    .foregroundColor(.primary)
                    .lineLimit(2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - STT Recording (Apple Speech framework — live transcription)

    /// Shared repo root resolution — canonical path for daemon cwd.
    /// Resolves symlinks and standardizes to get the true filesystem path
    /// (prevents case-mismatch issues on case-insensitive APFS volumes).
    ///
    /// Static form available for callers outside OverlayView (e.g. CompAppDelegate).
    static func resolveRepoRoot() -> URL {
        let raw: URL
        if let env = ProcessInfo.processInfo.environment["COMPY_ROOT"] {
            raw = URL(fileURLWithPath: env)
        } else {
            // #file is compy/swift/Sources/Compy/OverlayPanel.swift.
            // Walk up 5 levels to reach the project root.
            raw = URL(fileURLWithPath: #file)
                .deletingLastPathComponent()  // Sources/Compy
                .deletingLastPathComponent()  // Sources
                .deletingLastPathComponent()  // compy/swift
                .deletingLastPathComponent()  // compy
                .deletingLastPathComponent()  // project root
        }
        return raw.resolvingSymlinksInPath().standardized
    }

    private var repoRoot: URL { Self.resolveRepoRoot() }

    private func startRecording() {
        // Build on whatever is already typed or previously recognized.
        state.recognizedTextPrefix = state.text
        SFSpeechRecognizer.requestAuthorization { status in
            DispatchQueue.main.async {
                guard status == .authorized else {
                    self.state.isRecording = false
                    self.state.sttError = "Mic access denied — enable in System Settings > Privacy > Speech Recognition"
                    DispatchQueue.main.asyncAfter(deadline: .now() + 4) { self.state.sttError = nil }
                    return
                }
                self._beginRecognition()
            }
        }
    }

    private func stopRecording() {
        state.stopRecording()
    }

    private func _beginRecognition() {
        // Cancel any ongoing task before starting a new one.
        state.recognitionTask?.cancel()
        state.recognitionTask = nil

        state.recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
        guard let recognitionRequest = state.recognitionRequest else { return }

        let inputNode = state.audioEngine.inputNode
        recognitionRequest.shouldReportPartialResults = true

        state.isRecording = true
        state.sttError = nil

        state.recognitionTask = state.speechRecognizer.recognitionTask(with: recognitionRequest) { [weak state] result, error in
            guard let state = state else { return }

            if let result = result {
                let transcribed = result.bestTranscription.formattedString
                DispatchQueue.main.async {
                    // Show prefix + current partial transcription in real time.
                    state.text = [state.recognizedTextPrefix, transcribed]
                        .filter { !$0.isEmpty }
                        .joined(separator: " ")
                }
            }

            if error != nil || result?.isFinal == true {
                // Guard against double-fire: stopRecording() may have already run.
                guard state.recognitionRequest != nil else { return }

                DispatchQueue.main.async {
                    state.audioEngine.stop()
                    inputNode.removeTap(onBus: 0)
                    state.recognitionRequest = nil
                    state.recognitionTask = nil

                    if state.isRecording {
                        // User hasn't stopped — persist final text and restart
                        // for continuous listening until the mic button is clicked.
                        state.recognizedTextPrefix = state.text
                        // Brief backoff prevents a tight infinite loop on
                        // persistent mic/recognition errors.
                        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(400)) {
                            guard state.isRecording else { return }
                            self._beginRecognition()
                        }
                    } else {
                        state.mode = .text
                        if error != nil && state.text.isEmpty {
                            state.sttError = "Recognition error — try again"
                            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                                state.sttError = nil
                            }
                        }
                    }
                }
            }
        }

        let format = inputNode.outputFormat(forBus: 0)
        inputNode.removeTap(onBus: 0)  // ensure fresh tap when restarting
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak state] buffer, _ in
            state?.recognitionRequest?.append(buffer)
        }

        state.audioEngine.prepare()
        do {
            try state.audioEngine.start()
        } catch {
            state.audioEngine.stop()
            inputNode.removeTap(onBus: 0)
            state.recognitionRequest = nil
            state.recognitionTask = nil
            state.isRecording = false
            state.sttError = "Microphone unavailable — check mic permissions"
            DispatchQueue.main.asyncAfter(deadline: .now() + 4) { state.sttError = nil }
            return
        }
    }

    // MARK: - Content Area

    private var contentArea: some View {
        ZStack {
            switch state.phase {
            case .empty:
                emptyState
            case .processing:
                processingState
            case .results, .degraded:
                resultsView
            case .noMatch:
                noMatchState
            }
        }
        .frame(maxHeight: .infinity)
    }

    // MARK: - Empty State

    private var emptyState: some View {
        VStack(spacing: 18) {
            Image(systemName: "keyboard")
                .font(.system(size: 34, weight: .light))
                .foregroundColor(Color(NSColor.tertiaryLabelColor))

            HStack(spacing: 6) {
                Text("Press").foregroundColor(.secondary)
                HStack(spacing: 2) {
                    Image(systemName: "command")
                    Image(systemName: "shift")
                    Text("Space")
                }
                .font(.system(size: 12, weight: .semibold))
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .background(Color(NSColor.quaternaryLabelColor))
                .cornerRadius(4)
                Text("to ask about your code").foregroundColor(.secondary)
            }
            .font(.system(size: 13))

            Text("Esc or click outside to dismiss")
                .font(.system(size: 11))
                .foregroundColor(Color(NSColor.tertiaryLabelColor))
                .padding(.top, 4)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Processing State

    /// Randomized message pool — shuffles categories each search for variety.
    /// Three pools (intro, mid, outro) shuffled independently so the sequence
    /// always feels fresh but still tells a coherent progress story.
    private var progressMessages: [String] {
        let intros = [
            "Scanning the codebase...",
            "Reading through files...",
            "Exploring the repo...",
            "Gathering context...",
            "Looking around...",
        ].shuffled()
        let mids = [
            "Consulting the graph...",
            "Matching keywords...",
            "Running heuristics...",
            "Tracing symbols...",
            "Checking references...",
            "Following the trail...",
            "Connecting the dots...",
            "Mapping dependencies...",
        ].shuffled()
        let outros = [
            "Ranking results...",
            "Polishing...",
            "Almost there...",
        ].shuffled()
        // Pick 2 intros, 3 mids, 1 outro for a fresh but logical sequence.
        return [intros[0], intros[1], mids[0], mids[1], mids[2], outros[0]]
    }

    private var processingState: some View {
        TypingProgressView(messages: progressMessages)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - No Match State

    private var noMatchState: some View {
        VStack(spacing: 10) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 26, weight: .light))
                .foregroundColor(.secondary)
            Text("No results")
                .font(.system(size: 15, weight: .semibold))
                .foregroundColor(.primary)
            Text("Try rewording with more keywords,\nor include more surrounding code in your selection.")
                .font(.system(size: 12))
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 32)
            Text("Esc to dismiss")
                .font(.system(size: 11))
                .foregroundColor(Color(NSColor.tertiaryLabelColor))
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Results View

    private var resultsView: some View {
        VStack(spacing: 0) {
            if state.phase == .degraded {
                degradedBanner
            }

            // Result count + source badge
            resultHeader

            ScrollView {
                LazyVStack(spacing: 4) {
                    ForEach(state.results) { hit in
                        ResultRow(hit: hit)
                    }
                }
                .padding(12)
            }
        }
    }

    // MARK: - Result Header

    private var resultHeader: some View {
        HStack {
            Text("\(state.results.count) result\(state.results.count == 1 ? "" : "s")")
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(.secondary)

            Spacer()

            Text(state.sourceLabel.capitalized)
                .font(.system(size: 11, weight: .semibold))
                .foregroundColor(sourceBadgeColor)
                .padding(.horizontal, 7)
                .padding(.vertical, 2)
                .background(sourceBadgeColor.opacity(0.12))
                .cornerRadius(4)
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 4)
    }

    private var sourceBadgeColor: Color {
        switch state.sourceLabel {
        case "freebuff", "ollama": return .green
        case "heuristic": return .blue
        default: return .orange
        }
    }

    // MARK: - Degraded Banner

    private var degradedBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 12))
                .foregroundColor(.orange)
            Text(state.reasonText ?? "Results from text search only — ranking unavailable.")
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(.primary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.15))
        .cornerRadius(8)
        .padding(.horizontal, 12)
        .padding(.top, 12)
    }

    // MARK: - Daemon Call

    private func submitQuery() {
        guard !state.text.isEmpty else { return }
        let question = state.text

        withAnimation(.easeOut(duration: 0.15)) {
            state.phase = .processing
            state.results = []
            state.reasonText = nil
        }

        DispatchQueue.global(qos: .userInitiated).async {
            // Use the extension's workspaceRoot when valid (points to the user's
            // actual target project in agy-ide), falling back to repoRoot only
            // when the extension value is missing, empty, or not a real directory.
            var activeWorkspace = repoRoot.path
            if let extRoot = state.workspaceRoot, !extRoot.isEmpty {
                var isDir: ObjCBool = false
                if FileManager.default.fileExists(atPath: extRoot, isDirectory: &isDir), isDir.boolValue {
                    activeWorkspace = extRoot
                }
            }
            // Always send a Selection so the daemon receives the correct
            // workspace_root — even when no text/file was selected.
            let sel = Selection(
                text: state.selectionText,
                file: state.selectionFile,
                line: state.selectionLine,
                workspaceRoot: activeWorkspace
            )

            let request = QueryRequest(question: question, selection: sel)
            guard let jsonData = try? compyEncoder.encode(request) else {
                _debugLog("FAIL: JSON encode failed for question='\(question)'")
                DispatchQueue.main.async {
                    withAnimation(.easeOut(duration: 0.2)) { state.phase = .noMatch }
                }
                return
            }

            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["python3", "-m", "compy.daemon"]
            process.currentDirectoryURL = repoRoot

            let stdinPipe = Pipe()
            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            process.standardInput = stdinPipe
            process.standardOutput = stdoutPipe
            process.standardError = stderrPipe

            _debugLog("spawning daemon workspace=\(activeWorkspace) cwd=\(repoRoot.path) question='\(question)'")

            do {
                let lock = NSLock()
                var allOutput = Data()
                let done = DispatchSemaphore(value: 0)
                stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
                    let chunk = handle.availableData
                    if chunk.isEmpty {
                        handle.readabilityHandler = nil
                        done.signal()
                    } else {
                        lock.lock()
                        allOutput.append(chunk)
                        lock.unlock()
                    }
                }

                try process.run()
                try stdinPipe.fileHandleForWriting.write(contentsOf: jsonData)
                try stdinPipe.fileHandleForWriting.close()
                process.waitUntilExit()
                done.wait()
                lock.lock()
                let outData = allOutput
                lock.unlock()

                let exitCode = process.terminationStatus
                let stderrData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
                let stderrStr = String(data: stderrData, encoding: .utf8) ?? ""

                _debugLog("daemon exit=\(exitCode) outBytes=\(outData.count) stderr=\(stderrStr)")

                if exitCode != 0 {
                    let reason = stderrStr.isEmpty ? "daemon exit \(exitCode)" : "daemon: \(stderrStr)"
                    DispatchQueue.main.async {
                        withAnimation(.easeOut(duration: 0.2)) {
                            state.phase = .noMatch
                            state.reasonText = reason
                        }
                    }
                    return
                }

                if let result = try? compyDecoder.decode(QueryResult.self, from: outData) {
                    DispatchQueue.main.async {
                        withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                            state.results = result.hits
                            state.reasonText = result.reason
                            if result.hits.isEmpty {
                                state.phase = .noMatch
                            } else {
                                state.phase = result.degraded ? .degraded : .results
                            }
                        }
                    }
                } else {
                    let preview = String(data: outData.prefix(200), encoding: .utf8) ?? "<binary>"
                    _debugLog("FAIL: JSON decode failed. outData preview: \(preview)")
                    DispatchQueue.main.async {
                        withAnimation(.easeOut(duration: 0.2)) { state.phase = .noMatch }
                    }
                }
            } catch {
                _debugLog("FAIL: exception: \(error.localizedDescription)")
                DispatchQueue.main.async {
                    withAnimation(.easeOut(duration: 0.2)) { state.phase = .noMatch }
                }
            }
        }
    }

    /// Writes a timestamped diagnostic line to /tmp/compy-debug.log for debugging.
    private func _debugLog(_ message: String) {
        let ts = ISO8601DateFormatter().string(from: Date())
        let line = "[\(ts)] \(message)\n"
        if let data = line.data(using: .utf8) {
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
}

// MARK: - Result Row

struct ResultRow: View {
    let hit: RankedHit
    @State private var isHovered = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("\(hit.file):\(hit.line)")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.primary)
                    .lineLimit(1)
                Spacer()
                Text("via \(hit.source)")
                    .font(.system(size: 10))
                    .foregroundColor(Color(NSColor.tertiaryLabelColor))
                Text("\(Int(hit.score * 100))%")
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(scoreColor(hit.score))
            }
            Text(hit.snippet.trimmingCharacters(in: .whitespacesAndNewlines))
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(.secondary)
                .lineLimit(2)
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(NSColor.textBackgroundColor))
                .cornerRadius(4)
        }
        .padding(8)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(isHovered
                    ? Color(NSColor.quaternaryLabelColor)
                    : Color.clear)
        )
        .onHover { hovering in
            isHovered = hovering
            if hovering {
                NSCursor.pointingHand.push()
            } else {
                NSCursor.pop()
            }
        }
        .onTapGesture {
            EditorOpener.open(file: hit.file, line: hit.line)
        }
    }

    private func scoreColor(_ score: Double) -> Color {
        if score >= 0.9 { return .green }
        if score >= 0.6 { return .orange }
        return .secondary
    }
}

// MARK: - Typing Progress View

/// Animated typing indicator that cycles through messages character-by-character
/// with a blinking cursor. Replaces the static spinner.
struct TypingProgressView: View {
    let messages: [String]

    @State private var messageIndex: Int = 0
    @State private var charCount: Int = 0
    @State private var showCursor: Bool = false
    @State private var isActive: Bool = true

    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 0) {
                Text(currentDisplay)
                    .font(.system(size: 14, weight: .medium, design: .monospaced))
                    .foregroundColor(.secondary)
                Rectangle()
                    .fill(showCursor ? Color.accentColor : Color.clear)
                    .frame(width: 8, height: 16)
            }
        }
        .onAppear {
            isActive = true
            startTyping()
            startCursorBlink()
        }
        .onDisappear {
            isActive = false
        }
    }

    private var currentMessage: String {
        guard !messages.isEmpty else { return "Working..." }
        return messages[messageIndex % messages.count]
    }

    private var currentDisplay: String {
        String(currentMessage.prefix(min(charCount, currentMessage.count)))
    }

    private func startCursorBlink() {
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { timer in
            guard isActive else { timer.invalidate(); return }
            withAnimation(.easeInOut(duration: 0.15)) {
                showCursor.toggle()
            }
        }
    }

    private func startTyping() {
        charCount = 0
        Timer.scheduledTimer(withTimeInterval: 0.04, repeats: true) { timer in
            guard isActive else { timer.invalidate(); return }
            if charCount < currentMessage.count {
                charCount += 1
            } else {
                timer.invalidate()
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) {
                    guard isActive else { return }
                    eraseThenNext()
                }
            }
        }
    }

    private func eraseThenNext() {
        guard isActive else { return }
        if charCount > 0 {
            charCount -= 1
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(18)) {
                eraseThenNext()
            }
        } else {
            messageIndex += 1
            startTyping()
        }
    }
}
