// OverlayPanel.swift — Compy macOS overlay
//
// Native SwiftUI overlay: NSPanel, top-right, always-on-top, Cmd+Shift+Space trigger.
// Implements the full §3 state machine: mic/text modes, query submission via daemon
// subprocess, results display with degraded/no-match states, click-to-open in editor,
// Esc/click-outside dismiss, reasoner-source badge, and Compy personality system.

import SwiftUI
import AppKit
import Carbon.HIToolbox.Events

// MARK: - Compy Message Pool (Personality System)

/// Cooldown-backed message pools for all user-facing copy.
/// Each category tracks last-picked index + timestamp in UserDefaults.
/// Never repeats the same message within a 30-minute window.
/// Uses weighted selection: 80% standard phrasing, 20% personality.
struct CompyMessagePool {
    private static let cooldownKey = "compy.pool.cooldowns"
    private static let cooldownMinutes: TimeInterval = 30 * 60

    // MARK: - Pools

    /// Greetings shown as placeholder text when the overlay opens.
    static let greetings: [String] = [
        "Ask me anything about your code…",
        "What are you looking for?",
        "Search your codebase…",
        "Find a function, trace a call…",
        "Where is that thing again?",
        "Codebase at your fingertips…",
        "Ask away — I'll find it.",
        "What should I find for you?",
    ]

    /// Playful result headers — 20% chance instead of "N results".
    static let resultHeaders: [String] = [
        "Right where you left it — %d match%@",
        "Found it lurking in %@: %d hit%@",
        "Ah, there you are — %d result%@",
        "%d reference%@, served up.",
    ]

    /// Varied no-match hints — pool instead of static text.
    static let noMatchHints: [String] = [
        "Try rewording with more keywords,\nor include more surrounding code in your selection.",
        "Nothing turned up — try different words,\nor select the function name before searching.",
        "Hmm, no matches. Broader keywords?\nOr try selecting the symbol itself.",
        "No luck — the code might use different terms.\nTry describing what it does instead.",
        "Nothing found. Maybe the function lives\nin a different file? Try without the selection.",
        "Zero hits. Sometimes a shorter query\nwith just the symbol name works best.",
    ]

    // MARK: - Picker

    /// Pick a message from a pool, avoiding repeats within the cooldown window.
    /// Fallback: if all messages are on cooldown, pick the one with the oldest timestamp.
    static func pick(from pool: [String], category: String) -> String {
        guard !pool.isEmpty else { return "" }
        var cooldowns = loadCooldowns(for: category)
        let now = Date().timeIntervalSince1970

        // Find messages not on cooldown
        let available = pool.enumerated().filter { idx, _ in
            guard let ts = cooldowns[idx] else { return true }
            return (now - ts) >= cooldownMinutes
        }

        let chosen: Int
        if let pick = available.randomElement() {
            chosen = pick.offset
        } else {
            // All on cooldown — pick the oldest
            chosen = cooldowns.min(by: { ($0.value ) < ($1.value ) })?.key ?? Int.random(in: 0..<pool.count)
        }

        cooldowns[chosen] = now
        saveCooldowns(cooldowns, for: category)
        return pool[chosen]
    }

    /// Returns true ~20% of the time for personality seasoning.
    static func shouldUsePersonality() -> Bool {
        Int.random(in: 0..<5) == 0
    }

    // MARK: - UserDefaults

    private static func loadCooldowns(for category: String) -> [Int: TimeInterval] {
        guard let data = UserDefaults.standard.data(forKey: "\(cooldownKey).\(category)"),
              let dict = try? JSONDecoder().decode([String: TimeInterval].self, from: data)
        else { return [:] }
        var result: [Int: TimeInterval] = [:]
        for (key, value) in dict {
            guard let intKey = Int(key) else { continue }
            result[intKey] = value
        }
        return result
    }

    private static func saveCooldowns(_ dict: [Int: TimeInterval], for category: String) {
        let stringKeyed = dict.reduce(into: [:]) { $0[String($1.key)] = $1.value }
        guard let data = try? JSONEncoder().encode(stringKeyed) else { return }
        UserDefaults.standard.set(data, forKey: "\(cooldownKey).\(category)")
    }
}

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
        state.reset()
        // Pick a fresh greeting each time the overlay opens.
        state.greeting = CompyMessagePool.pick(from: CompyMessagePool.greetings, category: "greetings")

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
    @Published var sttPhase: String = ""  // "Recording…" / "Transcribing…" during whisper.cpp run

    /// Fresh greeting each overlay open — shown as input bar placeholder.
    @Published var greeting: String = "Ask me anything about your code…"

    /// Personality-flavored result header text (set when results come in, 20% chance).
    @Published var resultHeaderText: String = ""

    /// No-match hint — picked from pool each time no-match shows.
    @Published var noMatchHint: String = ""

    var selectionText: String = ""
    var selectionFile: String? = nil
    var selectionLine: Int? = nil
    var workspaceRoot: String? = nil

    /// Active whisper.cpp subprocess — nil when not recording.
    var sttProcess: Process?

    // MARK: - Face-state system (Compy personality mascot)

    /// Minimum time a face must remain visible before transitioning (anti-flicker).
    private static let faceMinimumDisplay: TimeInterval = 0.55

    /// When the current face was first shown — used to enforce minimum display floor.
    private var faceShownAt: Date = Date.distantPast

    /// The currently displayed face — crossfades with spring bounce on change.
    @Published var displayedFace: String = ">-<"

    /// Spring-bounce scale: 1.0 = normal, pops to 1.25 on transition then settles.
    @Published var faceScale: CGFloat = 1.0

    /// Opacity for crossfade transitions.
    @Published var faceOpacity: Double = 1.0

    /// Gentle vertical float offset for idle animation.
    @Published var faceFloatOffset: CGFloat = 0

    /// Shadow radius that pulses with state.
    @Published var faceGlowRadius: CGFloat = 0

    /// The intended face for the current pipeline phase (without flicker protection).
    var intendedFace: String {
        switch phase {
        case .empty: return ">-<"
        case .processing: return selectionText.isEmpty ? ">•_•<" : ">.>"
        case .results: return ">o<"
        case .noMatch: return ">!?<"
        case .degraded: return ">x_<"
        }
    }

    /// Color for the current face — meaningful per state.
    var faceColor: Color {
        switch phase {
        case .empty: return Color(NSColor.tertiaryLabelColor)
        case .processing: return .blue
        case .results: return .green
        case .noMatch: return .orange
        case .degraded: return .red
        }
    }

    /// Glow color — matches face but more saturated.
    var faceGlowColor: Color {
        faceColor.opacity(0.3)
    }

    /// Stop the processing pulse (called when results arrive).
    func stopProcessingPulse() {
        withAnimation(.easeOut(duration: 0.2)) {
            faceScale = 1.0
        }
    }

    /// Call on every state change — crossfade + spring bounce to new face.
    func maybeTransitionFace() {
        guard !faceTransitioning else { return }
        let target = intendedFace
        guard target != displayedFace else { return }
        faceTransitioning = true
        let elapsed = Date().timeIntervalSince(faceShownAt)
        let delay = max(0, Self.faceMinimumDisplay - elapsed)

        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self else { return }
            let targetNow = self.intendedFace
            guard targetNow != self.displayedFace else { return }

            // Crossfade: fade out → change face → spring bounce in
            withAnimation(.easeOut(duration: 0.12)) {
                self.faceOpacity = 0
                self.faceScale = 0.7
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
                self.displayedFace = targetNow
                withAnimation(.spring(response: 0.35, dampingFraction: 0.55)) {
                    self.faceOpacity = 1
                    self.faceScale = 1.25  // overshoot
                    self.faceGlowRadius = 8
                }
                // Settle back to normal
                withAnimation(.spring(response: 0.4, dampingFraction: 0.6).delay(0.15)) {
                    self.faceScale = 1.0
                }
                withAnimation(.easeOut(duration: 0.8).delay(0.3)) {
                    self.faceGlowRadius = 0
                }
                self.faceShownAt = Date()
                // Release the transition guard after settle.
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                    self?.faceTransitioning = false
                }
            }
        }
    }

    /// Start continuous idle float animation when overlay opens.
    func startIdleFloat() {
        withAnimation(.easeInOut(duration: 2.0).repeatForever(autoreverses: true)) {
            faceFloatOffset = -2
        }
    }

    /// Stop idle float (e.g. when query submitted).
    func stopIdleFloat() {
        withAnimation(.easeOut(duration: 0.3)) {
            faceFloatOffset = 0
        }
    }

    /// Guard against double-bounce from rapid successive transitions.
    private var faceTransitioning = false

    /// Continuous gentle pulse while processing.
    func startProcessingPulse() {
        withAnimation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true)) {
            faceScale = 1.06
        }
    }

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
        sttPhase = ""
        resultHeaderText = ""
        displayedFace = ">-<"
        faceScale = 1.0
        faceOpacity = 1.0
        faceFloatOffset = 0
        faceGlowRadius = 0
        faceShownAt = Date.distantPast
        faceTransitioning = false
        // noMatchHint picked lazily when .noMatch displays
        noMatchHint = ""
    }

    /// Kill the whisper.cpp subprocess if running.
    func stopRecording() {
        sttProcess?.terminate()
        sttProcess = nil
        isRecording = false
        sttPhase = ""
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
        ZStack(alignment: .topTrailing) {
            VStack(spacing: 0) {
                inputBar
                if shouldShowContent {
                    Divider()
                    contentArea
                }
            }
            .frame(width: 600)
            .frame(minHeight: 72)

            // Compy face — top-right, mirrors the macOS close button top-left.
            // Always visible regardless of compact/expanded state.
            compyFace
        }
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
            state.maybeTransitionFace()
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
            state.stopRecording()
            if let monitor = escMonitor {
                NSEvent.removeMonitor(monitor)
                escMonitor = nil
            }
        }
    }

    // MARK: - Input Bar

    /// ASCII face-state mascot — lives top-right, mirrors the macOS close button.
    /// Changes with pipeline phase (idle → thinking → found → confused → error).
    /// Color-coded, spring-bouncing, gently animated. The face IS the status indicator.
    private var compyFace: some View {
        Text(state.displayedFace)
            .font(.system(size: 18, weight: .medium, design: .monospaced))
            .foregroundColor(state.faceColor)
            .scaleEffect(state.faceScale)
            .opacity(state.faceOpacity)
            .offset(y: state.faceFloatOffset)
            .shadow(color: state.faceGlowColor, radius: state.faceGlowRadius, x: 0, y: 0)
            .padding(.top, 6)
            .padding(.trailing, 10)
    }

    // MARK: - Input Bar

    private var inputBar: some View {
        HStack(spacing: 12) {
            // Mic button — triggers whisper.cpp recording or switches to text mode.
            micButton

            if state.mode == .mic && !isFocused && state.text.isEmpty && !state.isRecording {
                Text(state.greeting)
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

    // MARK: - Mic Button (whisper.cpp STT)

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
            ZStack {
                // Expanding rings when recording — concentric pulse
                if state.isRecording {
                    ForEach(0..<3, id: \.self) { i in
                        Circle()
                            .stroke(Color.red.opacity(0.3), lineWidth: 1.5)
                            .frame(width: 14 + CGFloat(i) * 6, height: 14 + CGFloat(i) * 6)
                            .scaleEffect(state.isRecording ? 1.8 : 1.0)
                            .opacity(state.isRecording ? 0 : 0.5)
                            .animation(
                                state.isRecording
                                    ? .easeOut(duration: 1.2).repeatForever(autoreverses: false).delay(Double(i) * 0.25)
                                    : .none,
                                value: state.isRecording
                            )
                    }
                }
                Image(systemName: micButtonIcon)
                    .font(.system(size: 16, weight: .medium))
                    .foregroundColor(micButtonColor)
            }
            .frame(width: 28, height: 28)
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

    // MARK: - Recording Indicator

    private var recordingIndicator: some View {
        HStack(spacing: 8) {
            // Pulsing red dot
            Circle()
                .fill(Color.red)
                .frame(width: 8, height: 8)
                .scaleEffect(1.3)
                .animation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true), value: state.isRecording)
            if state.text.isEmpty {
                Text(state.sttPhase.isEmpty ? "Listening…" : state.sttPhase)
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

    // MARK: - STT Recording (whisper.cpp via daemon subprocess)

    /// Shared repo root resolution — canonical path for daemon cwd.
    static func resolveRepoRoot() -> URL {
        let raw: URL
        if let env = ProcessInfo.processInfo.environment["COMPY_ROOT"] {
            raw = URL(fileURLWithPath: env)
        } else {
            // #file is compy/swift/Sources/Compy/OverlayPanel.swift.
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

    /// Spawn whisper.cpp recording: burst-capture 4s of mic audio,
    /// transcribe, set text. Push-to-talk model — click to record, click to stop.
    /// Continuous: restarts after each burst with 400ms backoff.
    private func startRecording() {
        state.recognizedTextPrefix = state.text
        state.isRecording = true
        state.sttError = nil
        state.sttPhase = "Recording 4s…"
        _runWhisperBurst()
    }

    private func stopRecording() {
        state.stopRecording()
    }

    /// Run one whisper.cpp burst: spawn python3 -m compy.daemon.stt --duration 4,
    /// parse JSON, set text, optionally restart.
    private func _runWhisperBurst() {
        DispatchQueue.global(qos: .userInitiated).async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            proc.arguments = ["python3", "-m", "compy.daemon.stt", "--duration", "4"]
            proc.currentDirectoryURL = repoRoot

            let stdoutPipe = Pipe()
            proc.standardOutput = stdoutPipe
            proc.standardError = FileHandle.nullDevice

            // Store on the calling thread so stopRecording() can find it even
            // before the main-queue dispatch below fires. Not @Published, safe.
            state.sttProcess = proc

            proc.terminationHandler = { [weak state] _ in
                guard let state = state else { return }
                let data = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
                let jsonStr = String(data: data, encoding: .utf8) ?? ""

                DispatchQueue.main.async {
                    guard state.isRecording else { return }

                    if let jsonData = jsonStr.data(using: .utf8),
                       let result = try? JSONDecoder().decode(STTResult.self, from: jsonData) {
                        if result.success, !result.text.isEmpty {
                            // Append burst result to accumulated text
                            state.text = [state.recognizedTextPrefix, result.text]
                                .filter { !$0.isEmpty }
                                .joined(separator: " ")
                            state.recognizedTextPrefix = state.text
                            state.sttPhase = ""
                        } else if let err = result.error, !err.isEmpty {
                            state.sttError = err
                        }
                    } else if !jsonStr.isEmpty {
                        // whisper-cli may output raw text without JSON wrapper.
                        // Strip Metal/ggml init lines that leak to stdout.
                        let cleaned = jsonStr
                            .split(separator: "\n")
                            .filter { !$0.hasPrefix("ggml_") && !$0.hasPrefix("load_backend") }
                            .joined(separator: " ")
                            .trimmingCharacters(in: .whitespacesAndNewlines)
                        if !cleaned.isEmpty {
                            state.text = [state.recognizedTextPrefix, cleaned]
                                .filter { !$0.isEmpty }
                                .joined(separator: " ")
                            state.recognizedTextPrefix = state.text
                            state.sttPhase = ""
                        }
                    }

                    // Restart for continuous listening
                    if state.isRecording {
                        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(400)) {
                            guard state.isRecording else { return }
                            state.sttPhase = "Recording 4s…"
                            self._runWhisperBurst()
                        }
                    } else {
                        state.mode = .text
                        if state.sttError != nil && state.text.isEmpty {
                            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                                state.sttError = nil
                            }
                        }
                    }
                }
            }

            do {
                try proc.run()
            } catch {
                DispatchQueue.main.async {
                    state.isRecording = false
                    state.sttPhase = ""
                    state.sttError = "whisper.cpp unavailable — install with: brew install whisper-cpp"
                    state.mode = .text
                    DispatchQueue.main.asyncAfter(deadline: .now() + 4) { state.sttError = nil }
                }
            }
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
        VStack(spacing: 14) {
            // Compy face center-stage — large, the focal point of the overlay.
            Text(state.displayedFace)
                .font(.system(size: 48, weight: .light, design: .monospaced))
                .foregroundColor(state.faceColor)
                .scaleEffect(state.faceScale)
                .offset(y: state.faceFloatOffset)
                .shadow(color: state.faceGlowColor, radius: state.faceGlowRadius, x: 0, y: 2)
                .onAppear { state.startIdleFloat() }
                .onDisappear { state.stopIdleFloat() }

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
    private var progressMessages: [String] {
        let intros = [
            "Scanning the codebase…",
            "Reading through files…",
            "Exploring the repo…",
            "Gathering context…",
            "Looking around…",
        ].shuffled()
        let mids = [
            "Consulting the graph…",
            "Matching keywords…",
            "Running heuristics…",
            "Tracing symbols…",
            "Checking references…",
            "Following the trail…",
            "Connecting the dots…",
            "Mapping dependencies…",
        ].shuffled()
        let outros = [
            "Ranking results…",
            "Polishing…",
            "Almost there…",
        ].shuffled()
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
            Text(state.noMatchHint)
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
                    ForEach(Array(state.results.enumerated()), id: \.element.id) { index, hit in
                        ResultRow(hit: hit, index: index)
                    }
                }
                .padding(12)
            }
        }
    }

    // MARK: - Result Header

    private var resultHeader: some View {
        HStack {
            Text(resultHeaderCopy)
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

    /// 80% standard count, 20% personality phrasing.
    private var resultHeaderCopy: String {
        if !state.resultHeaderText.isEmpty {
            return state.resultHeaderText
        }
        let n = state.results.count
        return "\(n) result\(n == 1 ? "" : "s")"
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

    // MARK: - Haptics

    /// Light tap — query submitted.
    private func hapticSubmit() {
        NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .default)
    }

    /// Double-tap — results ready.
    private func hapticSuccess() {
        NSHapticFeedbackManager.defaultPerformer.perform(.levelChange, performanceTime: .default)
    }

    /// Warning tap — no results or error.
    private func hapticNoMatch() {
        NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .default)
    }

    // MARK: - Daemon Call

    private func submitQuery() {
        guard !state.text.isEmpty else { return }
        guard state.phase != .processing else { return }  // prevent rapid-fire submits
        let question = state.text

        hapticSubmit()
        state.stopIdleFloat()
        state.startProcessingPulse()

        withAnimation(.easeOut(duration: 0.15)) {
            state.phase = .processing
            state.results = []
            state.reasonText = nil
            state.resultHeaderText = ""
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
                    hapticNoMatch()
                    state.noMatchHint = CompyMessagePool.pick(from: CompyMessagePool.noMatchHints, category: "noMatchHints")
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
                        hapticNoMatch()
                        state.noMatchHint = CompyMessagePool.pick(from: CompyMessagePool.noMatchHints, category: "noMatchHints")
                        withAnimation(.easeOut(duration: 0.2)) {
                            state.phase = .noMatch
                            state.reasonText = reason
                        }
                    }
                    return
                }

                if let result = try? compyDecoder.decode(QueryResult.self, from: outData) {
                    DispatchQueue.main.async {
                        state.stopProcessingPulse()
                        let n = result.hits.count
                        // 20% chance of personality-flavored result header
                        if n > 0 && CompyMessagePool.shouldUsePersonality() {
                            let template = CompyMessagePool.pick(from: CompyMessagePool.resultHeaders, category: "resultHeaders")
                            if template.contains("%@") {
                                let source = state.sourceLabel.capitalized
                                // Replace only the FIRST %@ (source name); keep %@ for plural "s".
                                if let range = template.range(of: "%@") {
                                    let replaced = template.replacingCharacters(in: range, with: source)
                                    state.resultHeaderText = String(format: replaced, n, n == 1 ? "" : "s")
                                } else {
                                    state.resultHeaderText = String(format: template, n, n == 1 ? "" : "s")
                                }
                            } else {
                                state.resultHeaderText = String(format: template, n, n == 1 ? "" : "s")
                            }
                        }
                        withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                            state.results = result.hits
                            state.reasonText = result.reason
                            if result.hits.isEmpty {
                                state.phase = .noMatch
                            } else {
                                state.phase = result.degraded ? .degraded : .results
                            }
                        }
                        if result.hits.isEmpty {
                            hapticNoMatch()
                            state.noMatchHint = CompyMessagePool.pick(from: CompyMessagePool.noMatchHints, category: "noMatchHints")
                        } else {
                            hapticSuccess()
                        }
                    }
                } else {
                    let preview = String(data: outData.prefix(200), encoding: .utf8) ?? "<binary>"
                    _debugLog("FAIL: JSON decode failed. outData preview: \(preview)")
                    DispatchQueue.main.async {
                        hapticNoMatch()
                        state.noMatchHint = CompyMessagePool.pick(from: CompyMessagePool.noMatchHints, category: "noMatchHints")
                        withAnimation(.easeOut(duration: 0.2)) { state.phase = .noMatch }
                    }
                }
            } catch {
                _debugLog("FAIL: exception: \(error.localizedDescription)")
                DispatchQueue.main.async {
                    hapticNoMatch()
                    state.noMatchHint = CompyMessagePool.pick(from: CompyMessagePool.noMatchHints, category: "noMatchHints")
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
    let index: Int
    @State private var isHovered = false
    @State private var appeared = false

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
                .lineLimit(isHovered ? 8 : 2)
                .padding(6)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(NSColor.textBackgroundColor))
                .cornerRadius(4)
                .animation(.easeOut(duration: 0.2), value: isHovered)
        }
        .padding(8)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(isHovered
                    ? Color(NSColor.quaternaryLabelColor)
                    : Color.clear)
        )
        .opacity(appeared ? 1 : 0)
        .offset(y: appeared ? 0 : 8)
        .onAppear {
            withAnimation(.spring(response: 0.35, dampingFraction: 0.8).delay(Double(index) * 0.04)) {
                appeared = true
            }
        }
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
        guard !messages.isEmpty else { return "Working…" }
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
