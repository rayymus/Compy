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

/// Opens a file at a given line in the user's editor.
///
/// Uses the editor's registered URL scheme (e.g. `antigravity-ide://file/...`)
/// via NSWorkspace — this talks directly to the existing editor instance via
/// LaunchServices, bypassing the Electron CLI subprocess that causes the
/// temporary-window flash.  Falls back to the CLI if the URL scheme is
/// unavailable or fails.
///
/// `basePath` must be set once at launch to the project root so that relative
/// file paths from the daemon are resolved to absolute paths.
struct EditorOpener {
    static var basePath: String = "/"

    /// Editor URL schemes in preference order (agy-ide, cursor, code).
    private static let urlSchemes: [(scheme: String, bundleID: String, cliPath: String)] = [
        ("antigravity-ide", "com.google.antigravity-ide",
         "\(NSHomeDirectory())/.antigravity-ide/antigravity-ide/bin/agy-ide"),
        ("cursor", "com.cursor.Cursor", "/usr/local/bin/cursor"),
        ("vscode", "com.microsoft.VSCode", "/usr/local/bin/code"),
    ]

    static func open(file: String, line: Int) {
        let resolved: String
        if file.hasPrefix("/") {
            resolved = file
        } else {
            resolved = URL(fileURLWithPath: file,
                           relativeTo: URL(fileURLWithPath: basePath)).path
        }

        // Strategy 1: URL scheme — talks directly to existing editor instance.
        // Zero subprocess, zero Electron helper, zero flash.
        let encodedPath = resolved.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? resolved
        for entry in urlSchemes {
            guard let url = URL(string: "\(entry.scheme)://file/\(encodedPath):\(line)") else { continue }
            NSWorkspace.shared.open(url)
            return
        }

        // Strategy 2: CLI fallback — only reached if no URL scheme is registered.
        DispatchQueue.global(qos: .userInitiated).async {
            for entry in urlSchemes {
                let path = entry.cliPath
                guard FileManager.default.isExecutableFile(atPath: path) else { continue }
                let proc = Process()
                proc.executableURL = URL(fileURLWithPath: path)
                proc.arguments = ["-r", "-g", "\(resolved):\(line)"]
                do {
                    try proc.run()
                    return
                } catch {
                    continue
                }
            }
            // Final fallback: open the file in the default app.
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
    let state = OverlayState()

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
        if let panel = panel {
            // Session memory: hotkey toggles visibility, not state.
            // If the panel is currently key (visible and focused), dismiss it.
            // If it's not key (hidden behind other windows or resigned),
            // bring it back — preserving all session state.
            if panel.isKeyWindow {
                hide()
            } else {
                panel.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
            }
        } else {
            show(selectedText: selectedText, file: file, line: line, workspaceRoot: workspaceRoot)
        }
    }

    // MARK: NSWindowDelegate — blur handling

    func windowDidResignKey(_ notification: Notification) {
        // Don't hide on blur — Compy stays pinned until Esc or X button.
        // hidesOnDeactivate is set to false so the panel remains visible
        // even when focus returns to the editor.
        // The user explicitly wants: "Clicking off (blur) of the overlay
        // should not remove the search or stop Compy."
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
    enum Phase { case empty, processing, results, noMatch, degraded, refactorProposal }

    @Published var mode: Mode = .mic
    @Published var phase: Phase = .empty
    @Published var text: String = ""
    @Published var results: [RankedHit] = []
    /// Previous query results — preserved during multi-turn sessions, shown dimmed.
    @Published var previousResults: [RankedHit] = []
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

    /// Smart suggestions from daemon on no-match (synonyms, selection hints).
    @Published var noMatchSuggestions: [String]? = nil
    /// Brief toast message shown in the input bar after commands like /workspace.
    @Published var toastMessage: String? = nil
    /// Staged refactor token — non-nil while showing a refactor proposal.
    @Published var refactorToken: String? = nil
    /// Files that would change in the current refactor proposal.
    @Published var refactorProposals: [FileProposal] = []

    /// The last-submitted query — captured so the session-export HTML can show
    /// what was actually searched for (state.text is cleared after submit).
    var lastQuestion: String = ""
    /// True when the companion extension is writing fresh envelopes.
    /// Set by HotkeyManager when it reads a fresh envelope file.
    var extensionConnected: Bool = false
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

    /// The base face expression — set by maybeTransitionFace during morphs.
    /// blink/wink/dart are applied on top via the computed displayedFace.
    @Published var baseFace: String = ">-<"

    /// True while eyes are closed during a blink (~80ms).
    @Published var isBlinking = false

    /// True during a one-sided wink (same 80ms as blink).
    @Published var isWinking = false
    /// false = left wink `;-<`, true = right wink `>-;`.
    @Published var winkRightSide = false

    /// Non-nil while eyes are darting — a complete face override.
    @Published var eyeDart: String? = nil

    /// Click reaction — a playful face shown briefly when user taps Compy.
    /// Takes priority over blink/wink/dart. Cleared after ~0.8s.
    @Published var reactionFace: String? = nil

    /// Idle mood — cycles through subtle expression variations during .empty.
    /// Separated from baseFace so morph transitions work between moods.
    @Published var idleMood: String = ">-<"

    /// The face the user sees — blink/wink/dart/reaction layered on top of baseFace.
    var displayedFace: String {
        if let reaction = reactionFace { return reaction }
        if isBlinking { return blinkVariant() }
        if isWinking { return winkRightSide ? ">-;" : ";-<" }
        if let dart = eyeDart { return dart }
        return baseFace
    }

    /// Timer for periodic blinking.
    private var blinkTimer: Timer?
    /// Timer for eye darting during processing.
    private var dartTimer: Timer?

    /// Returns the eyes-closed variant of baseFace for blinks.
    func blinkVariant() -> String {
        switch baseFace {
        case ">-<": return "-_-"
        case ">.<": return "-.-"
        case ">•_•<": return "-•_•-"
        case ">.>": return "-.-"
        case ">o<": return "-o-"
        case ">!?<": return "-!?-"
        case ">x_<": return "-x_-"
        case ">°°<": return "-°°-"   // heuristic tier
        case ">-o<": return "--o-"   // grep/stub tier
        case ">O<": return "-O-"    // graph tier
        case ">*<": return "-*-"    // git tier
        case ">v<": return "-v-"    // dead code
        case ">~<": return "-~-"    // convention/smug
        default: return baseFace
        }
    }

    /// Eye-dart faces for the processing state — cycles through looking directions.
    func dartFaces() -> [String] {
        switch baseFace {
        case ">.>": return ["<.<", "O.O", ">_>", "<_<"]
        case ">•_•<": return [">.>", "<.<", "O.O"]
        default: return []
        }
    }

    /// Subtle idle animation variants — Compy looks around naturally.
    /// Triggered every 5-14s during .empty phase with varied glance durations.
    /// Session 20: more varied glances + occasional double-take + longer holds.
    private var idleShiftTimer: Timer?

    func startIdleShifts() {
        scheduleIdleShift()
    }

    private func scheduleIdleShift() {
        let interval = TimeInterval.random(in: 5...14)
        idleShiftTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: false) { [weak self] _ in
            guard let self = self, self.phase == .empty else { return }
            // Varied idle behaviors — like a real living thing:
            //  - 35% quick glance (150-300ms): subtle eye dart
            //  - 25% long look (400-800ms): Compy stares at something
            //  - 10% double-take: glance → return → glance again
            //  - 15% mood shift: subtle expression change (bored, curious, etc.)
            //  - 10% micro-expression: involuntary emotional flash (70-120ms)
            //  - 5% slow blink: a relaxed, content blink
            let roll = Double.random(in: 0...1)
            if roll < 0.05 {
                self.performBlink()  // slow content blink
            } else if roll < 0.15 {
                self.performMicroExpression()  // involuntary twitch
            } else if roll < 0.30 {
                self.performMoodShift()  // subtle expression change
            } else if roll < 0.40 {
                self.performDoubleTake()
            } else if roll < 0.65 {
                self.performLongLook()
            } else {
                self.performQuickGlance()
            }
            self.scheduleIdleShift()
        }
    }

    /// Quick subtle glance — Compy's eyes flick to one side briefly.
    private func performQuickGlance() {
        let glances = ["<.<", ">.>", ">_>", "<_<", "o.o", "-'-"]
        let glance = glances.randomElement()!
        let holdDuration = TimeInterval.random(in: 0.15...0.30)
        withAnimation(.easeInOut(duration: 0.06)) { eyeDart = glance }
        DispatchQueue.main.asyncAfter(deadline: .now() + holdDuration) { [weak self] in
            withAnimation(.easeOut(duration: 0.08)) { self?.eyeDart = nil }
        }
    }

    /// Longer stare — Compy fixes gaze on something for a beat.
    private func performLongLook() {
        let glances = ["O.O", "<.<", ">.>", ">_>"]
        let glance = glances.randomElement()!
        let holdDuration = TimeInterval.random(in: 0.40...0.80)
        withAnimation(.easeInOut(duration: 0.10)) { eyeDart = glance }
        DispatchQueue.main.asyncAfter(deadline: .now() + holdDuration) { [weak self] in
            withAnimation(.easeOut(duration: 0.15)) { self?.eyeDart = nil }
        }
    }

    /// Double-take: glance → return → glance again.
    private func performDoubleTake() {
        let glances = ["<.<", ">.>", "O.O"]
        let first = glances.randomElement()!
        withAnimation(.easeInOut(duration: 0.06)) { eyeDart = first }
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(120)) { [weak self] in
            withAnimation(.easeOut(duration: 0.05)) { self?.eyeDart = nil }
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(80)) { [weak self] in
                let second = glances.randomElement()!
                withAnimation(.easeInOut(duration: 0.06)) { self?.eyeDart = second }
                DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(150)) { [weak self] in
                    withAnimation(.easeOut(duration: 0.08)) { self?.eyeDart = nil }
                }
            }
        }
    }

    /// Micro-expression: a brief emotional flash (70-120ms).
    /// Simulates an involuntary twitch, realization, or tiny reaction.
    /// Token-based invalidation prevents the flash from reappearing after
    /// a concurrent blink finishes (which takes priority in displayedFace).
    private var microExpressionToken: Int = 0

    private func performMicroExpression() {
        let flashes = [">_<", "o_o", ">*<", ">,<", "O_O", ">w<"]
        let flash = flashes.randomElement()!
        let token = microExpressionToken + 1
        microExpressionToken = token
        withAnimation(.easeInOut(duration: 0.04)) { eyeDart = flash }
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(Int.random(in: 70...120))) { [weak self] in
            guard let self = self, self.microExpressionToken == token else { return }
            withAnimation(.easeOut(duration: 0.06)) { self.eyeDart = nil }
        }
    }

    /// Mood shift: smoothly transition to a different idle expression.
    /// Cycles through subtle mood variations so Compy doesn't just stare blankly.
    private func performMoodShift() {
        let moods = [">-<", ">~<", ">v<", ">.<", ">°°<", ">u<"]
        let next = moods.filter { $0 != idleMood }.randomElement() ?? ">-<"
        idleMood = next
        maybeTransitionFace()
    }

    func stopIdleShifts() {
        idleShiftTimer?.invalidate()
        idleShiftTimer = nil
        eyeDart = nil
    }

    /// Bouncy intro: starts at 3.0, springs to 1.0 on first appear.
    /// Reset to 3.0 on each show() so every hotkey press gets the pop-in.
    @Published var panelScale: CGFloat = 3.0

    /// Scale: 1.0 = normal, dips to 0.92 during morph dissolve then eases back.
    @Published var faceScale: CGFloat = 1.0

    /// Opacity for crossfade transitions (and processing breathing).
    @Published var faceOpacity: Double = 1.0

    /// Gentle vertical float offset for idle animation.
    @Published var faceFloatOffset: CGFloat = 0

    /// Last query's parsed intent — set when daemon returns results.
    var lastIntent: String = ""

    /// Captured client-side intent guess at submit time — frozen before text clears.
    /// The computed `guessedIntent` reads `text` which is empty during processing.
    var submittedIntentGuess: String = "fuzzy"

    /// Quick client-side intent guess from question text + selection.
    /// Used only at submit time to freeze `submittedIntentGuess`.
    /// An approximation of what the parser will determine — the real intent
    /// arrives with the daemon result and is stored in `lastIntent`.
    func captureIntentGuess() -> String {
        let q = text.lowercased()
        if !selectionText.isEmpty {
            if q.contains("where") || q.contains("find") || q.contains("show") { return "references" }
            if q.contains("why") || q.contains("who") { return "history" }
            return "references"
        }
        if q.contains("how does") || q.contains("explain") || q.contains("overview") { return "overview" }
        if q.contains("why") || q.contains("who added") || q.contains("blame") { return "history" }
        if q.contains("calls") || q.contains("depends") || q.contains("imports") { return "relational" }
        return "fuzzy"
    }

    /// The intended face for the current pipeline phase (without flicker protection).
    /// Processing face uses client-side intent guess; results face blends
    /// backend tier with actual parsed intent.
    var intendedFace: String {
        switch phase {
        case .empty: return idleMood
        case .processing: return intentProcessingFace
        case .results: return intentResultsFace
        case .noMatch: return ">!?<"
        case .refactorProposal: return ">O<"
        case .degraded: return ">x_<"
        }
    }

    /// Processing face reflects what kind of search Compy THINKS it's doing.
    /// Uses captured guess frozen at submit time — text is empty during processing.
    private var intentProcessingFace: String {
        switch submittedIntentGuess {
        case "references", "definition": return ">.>"      // targeted lookup
        case "overview": return ">O<"                       // structural digest
        case "relational", "blast_radius": return ">O<"     // graph query
        case "history", "rationale": return ">*<"           // git archaeology
        default: return ">•_•<"                              // broad fuzzy search
        }
    }

    /// Results face blends backend tier (which answered) with intent (what was asked).
    /// Intent shapes the eyes; tier shapes the mouth/confidence markers.
    private var intentResultsFace: String {
        let intent = lastIntent.isEmpty ? "fuzzy" : lastIntent
        switch intent {
        case "references", "definition":
            return tierResultsFace  // already well-calibrated for find results
        case "overview":
            return ">O<"            // structural — always confident
        case "relational", "blast_radius":
            return ">O<"            // graph-derived
        case "history", "rationale":
            return ">*<"            // historical/git source
        case "trace":
            return ">o<"            // exact match — confident
        case "fuzzy":
            return tierResultsFace  // source-tier face: ollama >o<, heuristic >°°<, etc.
        case "dead_code":
            return ">v<"            // looking down — cleanup/suspicious
        // convention/dedup demoted to fuzzy in orchestrator — reserved faces:
        // case "convention", "dedup": return ">~<"
        default:
            return tierResultsFace
        }
    }

    /// Results face varies by which backend actually answered.
    /// Tier-of-origin: ollama gets full-confidence eyes, heuristic gets approximate,
    /// degraded gets the existing error face. trace source stays standard.
    private var tierResultsFace: String {
        switch sourceLabel {
        case "ollama", "freebuff": return ">o<"      // LLM ranked — confident found
        case "heuristic": return ">°°<"               // offline ranked — approximate match
        case "grep", "stub": return ">-o<"             // unranked / stub — uncertain find
        case "graph": return ">O<"                     // graph-derived — structural match
        case "trace": return ">o<"                     // stack trace — exact match
        case "git": return ">*<"                       // git history — historical info
        default: return ">o<"
        }
    }

    /// Color for the current face — meaningful per state.
    /// Tier-of-origin awareness: result colors reflect backend confidence tier.
    var faceColor: Color {
        switch phase {
        case .empty: return Color(NSColor.tertiaryLabelColor)
        case .processing: return .blue
        case .results:
            switch sourceLabel {
            case "ollama", "freebuff": return .green
            case "heuristic": return .mint
            case "grep", "stub": return .teal
            case "graph": return .cyan
            case "git": return .indigo
            case "trace": return .green
            default: return .green
            }
        case .noMatch: return .orange
        case .refactorProposal: return .cyan
        case .degraded: return .red
        }
    }

    /// Stop the processing pulse (called when results arrive).
    /// Resets faceTransitioning so the pending morph to results/noMatch face can proceed.
    func stopProcessingPulse() {
        faceTransitioning = false
        stopDartTimer()
        eyeDart = nil
        withAnimation(.easeOut(duration: 0.25)) {
            faceOpacity = 1.0
            faceScale = 1.0
        }
    }

    /// Call on every state change — smooth morph dissolve to new face.
    /// The face blinks during the swap so the new expression appears with "open eyes."
    func maybeTransitionFace() {
        guard !faceTransitioning else { return }
        let target = intendedFace
        guard target != baseFace else { return }
        faceTransitioning = true
        let elapsed = Date().timeIntervalSince(faceShownAt)
        let delay = max(0, Self.faceMinimumDisplay - elapsed)

        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self else { return }
            let targetNow = self.intendedFace
            guard targetNow != self.baseFace else { return }

            // Blink closed while the face swaps — eyes close during transition.
            self.isBlinking = true

            // Morph: dissolve out → swap face → ease back in.
            withAnimation(.easeOut(duration: 0.15)) {
                self.faceOpacity = 0
                self.faceScale = 0.92
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                self.baseFace = targetNow
                // Open eyes now that the new face is revealed.
                self.isBlinking = false
                withAnimation(.easeOut(duration: 0.25)) {
                    self.faceOpacity = 1
                    self.faceScale = 1.0
                }
                self.faceShownAt = Date()
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in
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
    /// fileprivate so OverlayView can check it in triggerClickReaction.
    fileprivate var faceTransitioning = false

    // MARK: - Blink & Dart Timers

    /// Start periodic blinking: every 2-6 seconds (mix of short and long intervals),
    /// eyes close over 30ms, hold 40ms, open over 50ms for organic feel.
    /// 15% chance to wink instead of full blink.
    /// ~5% chance of double-blink (two rapid blinks in succession).
    func startBlinkTimer() {
        stopBlinkTimer()
        scheduleNextBlink()
    }

    private func scheduleNextBlink() {
        // Mix of short (2-4s) and long (5-8s) intervals for natural rhythm.
        let isLongPause = Double.random(in: 0...1) < 0.30
        let interval: TimeInterval = isLongPause
            ? TimeInterval.random(in: 5...8)
            : TimeInterval.random(in: 2...4)
        blinkTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: false) { [weak self] _ in
            guard let self = self else { return }
            self.fireBlink()
        }
    }

    private func fireBlink() {
        // Don't blink during a morph transition — let the morph handle it.
        guard !faceTransitioning else { scheduleNextBlink(); return }
        // 15% chance to wink instead of full blink.
        if Double.random(in: 0...1) < 0.15 {
            performWink()
        } else {
            performBlink()
        }
        // ~5% chance of double-blink: rapid second blink after 120ms.
        if Double.random(in: 0...1) < 0.05 {
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(120)) { [weak self] in
                guard let self = self, !self.faceTransitioning else { return }
                self.performBlink()
            }
        }
        // Schedule the next blink.
        scheduleNextBlink()
    }

    /// Organic three-phase blink: close → hold → open.
    private func performBlink() {
        // Phase 1: close over 30ms
        withAnimation(.easeIn(duration: 0.03)) { isBlinking = true }
        // Phase 2: hold closed 40ms
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(70)) { [weak self] in
            guard let self = self else { return }
            // Phase 3: open over 50ms
            withAnimation(.easeOut(duration: 0.05)) { self.isBlinking = false }
        }
    }

    /// Wink: one eye closes with same organic timing.
    private func performWink() {
        winkRightSide = Bool.random()
        withAnimation(.easeIn(duration: 0.03)) { isWinking = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(70)) { [weak self] in
            guard let self = self else { return }
            withAnimation(.easeOut(duration: 0.05)) { self.isWinking = false }
        }
    }

    func stopBlinkTimer() {
        blinkTimer?.invalidate()
        blinkTimer = nil
        isBlinking = false
        isWinking = false
    }

    /// Start eye-darting: cycles through looking directions every 1.5-2.5s.
    func startDartTimer() {
        stopDartTimer()
        let faces = dartFaces()
        guard !faces.isEmpty else { return }
        scheduleNextDart(index: -1)
    }

    private func scheduleNextDart(index: Int) {
        let interval = TimeInterval.random(in: 1.5...2.5)
        dartTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: false) { [weak self] _ in
            guard let self = self else { return }
            let faces = self.dartFaces()
            guard !faces.isEmpty else { self.eyeDart = nil; return }
            let nextIdx = (index + 1) % (faces.count + 1)  // +1 for nil (base face)
            if nextIdx == faces.count {
                self.eyeDart = nil
                self.scheduleNextDart(index: -1)
            } else {
                self.eyeDart = faces[nextIdx]
                self.scheduleNextDart(index: nextIdx)
            }
        }
    }

    func stopDartTimer() {
        dartTimer?.invalidate()
        dartTimer = nil
        eyeDart = nil
    }

    // MARK: - Pulse

    /// Start the processing pulse — gentle opacity breathing + eye darts.
    /// Delayed until after the face morph settles so the opacity animation
    /// doesn't fight the morph's dissolve, and so baseFace is already the
    /// processing expression when dartFaces() is called.
    func startProcessingPulse() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            guard let self = self, self.phase == .processing else { return }
            withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true)) {
                self.faceOpacity = 0.6
            }
            self.startDartTimer()
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
        previousResults = []
        reasonText = nil
        isRecording = false
        sttError = nil
        sttPhase = ""
        resultHeaderText = ""
        baseFace = ">-<"
        isBlinking = false
        isWinking = false
        winkRightSide = false
        eyeDart = nil
        reactionFace = nil  // clear click reaction
        faceScale = 1.0
        faceOpacity = 1.0
        faceFloatOffset = 0
        idleMood = ">-<"  // reset idle mood
        faceShownAt = Date.distantPast
        faceTransitioning = false
        stopBlinkTimer()
        stopDartTimer()
        stopIdleShifts()
        refactorToken = nil
        refactorProposals = []
        noMatchSuggestions = nil
        // noMatchHint picked lazily when .noMatch displays
        noMatchHint = ""
        noMatchSuggestions = nil
        toastMessage = nil
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
    /// Track copy feedback: briefly show checkmark after copy.
    @State private var didCopy = false

    /// Only show the content area when there's something to display —
    /// keep the overlay compact (just the input bar) until a query is submitted.
    private var shouldShowContent: Bool {
        if state.phase == .empty { return false }
        return true
    }

    /// Placeholder hint that adapts to session state.
    /// When results are showing, prompt for a follow-up instead of the default.
    private var followUpPlaceholder: String {
        if state.phase == .results || state.phase == .degraded {
            return "Ask a follow-up…"
        } else if state.phase == .noMatch {
            return "Try a different query…"
        } else if state.phase == .refactorProposal {
            return "Press Enter to accept, Esc to reject…"
        }
        return "Search codebase..."
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            VStack(spacing: 0) {
                inputBar
                if shouldShowContent {
                    Divider()
                    contentArea
                }
            }
            .frame(width: 600)
            .frame(minHeight: 72)

            // Compy face — top-left, mirrors the macOS close button.
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
            guard state.phase != .results, state.phase != .degraded, state.phase != .refactorProposal else { return }
            guard !state.isRecording else { return }
            isFocused = false
            state.mode = .mic
        }
        .onAppear {
            state.startBlinkTimer()
            state.startIdleShifts()
            escMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
                if event.keyCode == UInt16(kVK_Escape) {
                    if state.phase == .refactorProposal {
                        rejectRefactor()
                        return nil
                    }
                    OverlayController.shared.hide()
                    return nil
                }
                return event
            }
        }
        .onDisappear {
            state.stopRecording()
            state.stopBlinkTimer()
            state.stopIdleShifts()
            if let monitor = escMonitor {
                NSEvent.removeMonitor(monitor)
                escMonitor = nil
            }
        }
    }

    // MARK: - Input Bar

    /// ASCII face-state mascot — lives top-left, near the macOS close button.
    /// Shows during .empty and .noMatch phases (the "no results" states).
    /// Hidden during .processing and .results/.degraded — the face moves to the
    /// content area for those phases.
    /// Per clarifications.md: ONE face at a time — either corner or content area.
    @ViewBuilder
    private var compyFace: some View {
        if (state.phase == .empty && introDone) || state.phase == .noMatch || state.phase == .refactorProposal {
            faceView(size: 18)
                .offset(y: state.faceFloatOffset)
                .padding(.top, 6)
                .padding(.leading, 52)  // clear of traffic-light buttons
        }
    }

    /// Reusable face view — size-scalable for different contexts (corner, empty state, results).
    /// Tapping Compy triggers a playful reaction — surprised face + bounce.
    private func faceView(size: CGFloat) -> some View {
        Text(state.displayedFace)
            .font(.system(size: size, weight: .medium, design: .monospaced))
            .foregroundColor(state.faceColor)
            .scaleEffect(state.faceScale)
            .opacity(state.faceOpacity)
            .onTapGesture {
                triggerClickReaction()
            }
    }

    /// Playful reaction when Compy is clicked — like poking a pet.
    private func triggerClickReaction() {
        // Don't react during processing (already animating) or during a morph transition
        // (the click's scale bounce would fight the morph's dissolve).
        guard state.phase != .processing, state.phase != .refactorProposal else { return }
        guard !state.faceTransitioning else { return }
        let reactions = ["^o^", ">w<", ">3<", "O_O", ">_<", "*o*"]
        let reaction = reactions.randomElement()!
        withAnimation(.spring(response: 0.2, dampingFraction: 0.5)) {
            state.reactionFace = reaction
            state.faceScale = 1.25
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) { [weak state] in
            withAnimation(.easeOut(duration: 0.3)) {
                state?.reactionFace = nil
                state?.faceScale = 1.0
            }
        }
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
                TextField(followUpPlaceholder, text: $state.text)
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
                        // Session memory: clearing text during results keeps results visible
                        // but resets to compact. User can type a new query.
                        if state.phase == .results || state.phase == .degraded {
                            state.previousResults = state.results
                            state.phase = .empty
                            state.results = []
                        } else {
                            state.phase = .empty
                            state.results = []
                            state.previousResults = []
                        }
                    }
                }) {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 14))
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
            }

            // Streaming indicator — compact progress badge during the ranking phase.
            // Shows while the daemon is running the reasoner on streamed candidates.
            if inputBarIsStreaming && inputBarStreamCount > 0 {
                HStack(spacing: 4) {
                    ProgressView()
                        .scaleEffect(0.55)
                        .frame(width: 10, height: 10)
                    Text("Ranking \(inputBarStreamCount)…")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(.blue)
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .background(Color.blue.opacity(0.08))
                .cornerRadius(4)
                .transition(.opacity.combined(with: .scale(scale: 0.95)))
            }

            // Extension + workspace indicator — shows connection status and project.
            // Green dot = extension is writing fresh envelopes.
            // Gray dot = using AX fallback (extension not detected).
            if let toast = state.toastMessage {
                HStack(spacing: 4) {
                    Text(toast)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(toast.hasPrefix("✓") ? .green : .orange)
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .background(toast.hasPrefix("✓") ? Color.green.opacity(0.08) : Color.orange.opacity(0.08))
                .cornerRadius(4)
                .transition(.opacity.combined(with: .scale(scale: 0.95)))
            } else if let root = state.workspaceRoot, !root.isEmpty {
                let name = URL(fileURLWithPath: root).lastPathComponent
                let hasExtension = state.extensionConnected
                HStack(spacing: 4) {
                    Circle()
                        .fill(hasExtension ? Color.green : Color.gray)
                        .frame(width: 6, height: 6)
                    Text("in \(name)/")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(Color(NSColor.tertiaryLabelColor))
                }
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .background(Color(NSColor.quaternaryLabelColor))
                .cornerRadius(4)
                .help(hasExtension
                    ? "Extension connected · searching \(root)"
                    : "Extension not detected · using \(root)")
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

    /// Resolve the active workspace for search — the directory ripgrep should scan.
    /// Priority: 1) extension's workspaceRoot (validated), 2) git root derived from
    /// selection file, 3) COMPY_ROOT fallback (compy/ itself).
    static func resolveActiveWorkspace(
        extRoot: String?,
        selectionFile: String?,
        fallbackRoot: String
    ) -> String {
        // 1) Extension provided a valid workspace — use it.
        //    Reject "/" (the extension's fallback when no workspace is open)
        //    so ripgrep never tries to search the entire filesystem root.
        if let root = extRoot, !root.isEmpty, root != "/" {
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: root, isDirectory: &isDir), isDir.boolValue {
                return root
            }
        }
        // 2) Walk up from selection file to find a git repo root.
        if let file = selectionFile, !file.isEmpty {
            var current = URL(fileURLWithPath: file).deletingLastPathComponent()
            for _ in 0..<8 {  // walk up at most 8 levels
                let gitDir = current.appendingPathComponent(".git")
                var isDir: ObjCBool = false
                if FileManager.default.fileExists(atPath: gitDir.path, isDirectory: &isDir) {
                    return current.path
                }
                // Also check if we hit the filesystem root.
                if current.path == "/" || current.pathComponents.count <= 1 {
                    break
                }
                current = current.deletingLastPathComponent()
            }
        }
        // 3) Fall back to COMPY_ROOT.
        return fallbackRoot
    }

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
            case .refactorProposal:
                refactorProposalView
            }
        }
        .frame(maxHeight: .infinity)
    }

    // MARK: - Empty State

    private var emptyState: some View {
        VStack(spacing: 14) {
            if !introDone {
                // Compy face pops in BIG and bouncy, then settles before the input bar appears.
                faceView(size: 48)
                    .scaleEffect(introFaceScale)
                    .padding(.top, 16)
                    .transition(.opacity)
                    .onAppear {
                        withAnimation(.spring(response: 0.4, dampingFraction: 0.6)) {
                            introFaceScale = 1.0
                        }
                        // After the bounce settles, show the input hint.
                        // 0.5s > 0.4s spring — ensures face has settled.
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                            withAnimation(.easeOut(duration: 0.25)) {
                                introDone = true
                            }
                            state.startIdleFloat()
                        }
                    }
            } else {
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
                .transition(.opacity.combined(with: .move(edge: .bottom).combined(with: .opacity)))

                Text("Esc or X to dismiss")
                    .font(.system(size: 11))
                    .foregroundColor(Color(NSColor.tertiaryLabelColor))
                    .padding(.top, 4)
                    .transition(.opacity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onDisappear { state.stopIdleFloat() }
    }

    // MARK: - Processing State

    /// Build a fresh shuffled progress-message sequence.
    /// Called once when processing starts so the typing animation stays stable
    /// across re-renders triggered by blink/wink/@Published state changes.
    private static func buildProgressMessages() -> [String] {
        let intros = [
            "Scanning the codebase…",
            "Reading through files…",
            "Exploring the repo…",
            "Gathering context…",
            "Looking around…",
            "Taking a peek…",
            "Let me check…",
            "On the hunt…",
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
            "Reading call sites…",
            "Indexing matches…",
            "Checking git history…",
            "Sorting by relevance…",
        ].shuffled()
        let outros = [
            "Ranking results…",
            "Polishing…",
            "Almost there…",
            "Just a moment…",
            "Finishing up…",
            "Wrapping up…",
        ].shuffled()
        // Rare personality quip (~10% chance) sprinkled in mid-sequence.
        let personality = [
            "Hmm, I know this one…",
            "Oh, interesting…",
            "Let me think…",
        ]
        var msgs = [intros[0], intros[1], mids[0], mids[1], mids[2], outros[0]]
        if Double.random(in: 0...1) < 0.10 {
            msgs.insert(personality.randomElement()!, at: msgs.count / 2)
        }
        return msgs
    }

    private var processingState: some View {
        VStack(spacing: 0) {
            // Compy face — the focal point during search.
            // Per clarifications.md: ONE face at a time — this replaces the corner badge.
            faceView(size: 28)
                .padding(.top, 20)
                .padding(.bottom, 8)
            
            TypingProgressView(messages: currentProgressMessages)
                .frame(maxWidth: .infinity)

            // Show previous results dimmed — session memory across queries.
            if !state.previousResults.isEmpty {
                Divider()
                    .padding(.horizontal, 12)
                HStack {
                    Text("Previous results")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundColor(Color(NSColor.tertiaryLabelColor))
                    Spacer()
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                ScrollView {
                    LazyVStack(spacing: 4) {
                        ForEach(Array(state.previousResults.enumerated()), id: \.element.id) { index, hit in
                            ResultRow(hit: hit, index: index)
                                .opacity(0.35)
                                .allowsHitTesting(false)  // prevent accidental clicks during processing
                        }
                    }
                    .padding(12)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - No Match State

    private var noMatchState: some View {
        VStack(spacing: 10) {
            // No face here — the corner badge in the ZStack overlay is the sole face.
            // Per clarifications.md: ONE face at a time, not doubling.
            
            Image(systemName: "magnifyingglass")
                .font(.system(size: 26, weight: .light))
                .foregroundColor(.secondary)
            Text("No results")
                .font(.system(size: 15, weight: .semibold))
                .foregroundColor(.primary)
            // Smart suggestions from daemon (synonyms, selection hints) —
            // contextual and specific, replaces static hint pool when available.
            if let suggestions = state.noMatchSuggestions, !suggestions.isEmpty {
                ForEach(suggestions, id: \.self) { suggestion in
                    HStack(spacing: 4) {
                        Image(systemName: "lightbulb")
                            .font(.system(size: 10))
                            .foregroundColor(.yellow)
                        Text(suggestion)
                            .font(.system(size: 12, weight: .medium))
                    }
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 4)
                    .background(Color.yellow.opacity(0.08))
                    .cornerRadius(6)
                }
            } else {
                Text(state.noMatchHint)
                    .font(.system(size: 12))
                    .foregroundColor(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)
            }
            Text("Esc to dismiss  ·  Type a new query to try again")
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

            // Compy face — prominent, the focal point of results.
            // Per clarifications.md: "either top left (no results) or in the search results."
            // This is the "in the search results" placement — large and visible.
            faceView(size: 28)
                .padding(.top, 12)
                .padding(.bottom, 4)

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

    /// True while the header still shows the "Ranking N candidates…" streaming message.
    /// Flips to false when final results arrive, triggering the source badge fade-in
    /// and the count transition.
    @State private var headerIsRanking = false

    /// True while the daemon is streaming intermediate grep candidates.
    /// Drives the compact "Ranking N candidates…" indicator in the input bar.
    @State private var inputBarIsStreaming = false
    /// Number of streamed candidates — shown in the input bar indicator.
    @State private var inputBarStreamCount = 0

    /// Stable shuffled progress messages — generated once when processing starts.
    /// Previously a computed property that called .shuffled() on every render,
    /// causing the typing animation to jump to random messages whenever any
    /// @Published state change triggered a re-render (e.g. isBlinking toggling).
    @State private var currentProgressMessages: [String] = []

    private var resultHeader: some View {
        HStack {
            Text(resultHeaderCopy)
                .font(.system(size: 12, weight: .medium))
                .foregroundColor(.secondary)
                .contentTransition(.opacity)

            Spacer()

            // Copy all results as formatted text — session-export for sharing.
            Button(action: copyResultsToClipboard) {
                Image(systemName: copyIcon)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(.secondary)
            }
            .buttonStyle(.plain)
            .help("Copy all results as formatted text")

            // Export session as animated HTML — shareable clip with Compy's personality.
            Button(action: exportSessionAsHTML) {
                Image(systemName: "square.and.arrow.up")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(.secondary)
            }
            .buttonStyle(.plain)
            .help("Export animated session clip as HTML")

            // Source badge — fades in when ranking completes.
            // Hidden during the "Ranking N candidates…" streaming phase.
            if !headerIsRanking && !state.sourceLabel.isEmpty {
                Text(state.sourceLabel.capitalized)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundColor(sourceBadgeColor)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(sourceBadgeColor.opacity(0.12))
                    .cornerRadius(4)
                    .transition(.opacity.combined(with: .scale(scale: 0.9)))
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 4)
        .animation(.easeInOut(duration: 0.35), value: headerIsRanking)
        .onChange(of: state.resultHeaderText) { _, newValue in
            // Detect the header phase by checking for the streaming prefix.
            // The daemon sets "Ranking N candidates…" during intermediate phase;
            // the final handler either sets a personality header or clears it.
            headerIsRanking = newValue.hasPrefix("Ranking ")
        }
    }

    /// Bouncy intro: face starts at 3x, springs to 1x before input bar appears.
    @State private var introFaceScale: CGFloat = 3.0
    /// True after the face bounce settles — reveals the input hints.
    @State private var introDone = false

    private var copyIcon: String {
        didCopy ? "checkmark" : "square.on.square"
    }

    /// Format all results as readable text and copy to system clipboard.
    private func copyResultsToClipboard() {
        let lines = state.results.map { hit in
            "\(hit.file):\(hit.line)  [\(hit.source) \(Int(hit.score * 100))%]\n  \(hit.snippet.trimmingCharacters(in: .whitespacesAndNewlines))"
        }
        let text = lines.joined(separator: "\n\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        // Brief checkmark feedback
        didCopy = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            didCopy = false
        }
    }

    /// Export the current session as a self-contained HTML file with CSS animations.
    /// Replays the query, Compy's face, and staggered results — a shareable clip
    /// that captures the "wow" moment (speed + personality together).
    private func exportSessionAsHTML() {
        let html = generateSessionHTML()
        let fileName = "compy-session-\(Int(Date().timeIntervalSince1970)).html"
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(fileName)
        try? html.write(to: url, atomically: true, encoding: .utf8)
        NSWorkspace.shared.open(url)
    }

    /// Build a self-contained HTML document that replays the current session.
    /// Includes Compy's face, the query, staggered results, source badge, and
    /// CSS keyframe animations that mirror the native SwiftUI stagger + morph.
    private func generateSessionHTML() -> String {
        let now = ISO8601DateFormatter().string(from: Date())
        let n = state.results.count
        let sourceLabel = state.sourceLabel.capitalized
        let escapedFace = state.displayedFace
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
        let queryText = state.lastQuestion.isEmpty ? "Search codebase..." : state.lastQuestion
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
        let titleQuery = state.lastQuestion.prefix(60)
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")

        // Build result rows with staggered animation delays.
        var resultRows = ""
        for (i, hit) in state.results.enumerated() {
            let delay = Double(i) * 0.04
            let scorePct = Int(hit.score * 100)
            let escapedFile = hit.file
                .replacingOccurrences(of: "&", with: "&amp;")
                .replacingOccurrences(of: "<", with: "&lt;")
                .replacingOccurrences(of: ">", with: "&gt;")
            let escapedSource = hit.source
                .replacingOccurrences(of: "&", with: "&amp;")
                .replacingOccurrences(of: "<", with: "&lt;")
                .replacingOccurrences(of: ">", with: "&gt;")
            let escapedSnippet = hit.snippet
                .replacingOccurrences(of: "&", with: "&amp;")
                .replacingOccurrences(of: "<", with: "&lt;")
                .replacingOccurrences(of: ">", with: "&gt;")
                .replacingOccurrences(of: "\"", with: "&quot;")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let ctxBadge: String
            if let ctx = hit.structuralContext, !ctx.isEmpty {
                let escapedCtx = ctx
                    .replacingOccurrences(of: "&", with: "&amp;")
                    .replacingOccurrences(of: "<", with: "&lt;")
                    .replacingOccurrences(of: ">", with: "&gt;")
                ctxBadge = "<span class='struct-badge'>\(escapedCtx)</span>"
            } else {
                ctxBadge = ""
            }
            let scoreClass = scorePct >= 90 ? "score-high" : (scorePct >= 60 ? "score-mid" : "score-low")
            resultRows += """
            <div class="result-row" style="animation-delay: \(delay)s">
              <div class="result-header">
                <span class="file-line">\(escapedFile):\(hit.line)</span>
                <span class="result-source">via \(escapedSource)</span>
                <span class="result-score \(scoreClass)">\(scorePct)%</span>
              </div>
              <div class="result-snippet">\(escapedSnippet)</div>
              \(ctxBadge)
            </div>
            """
        }

        return """
        <!DOCTYPE html>
        <html lang="en">
        <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Compy: \(titleQuery)</title>
        <style>
          :root {
            --bg: #1e1e2e;
            --surface: #2a2a3c;
            --text: #cdd6f4;
            --muted: #6c7086;
            --accent: #89b4fa;
            --green: #a6e3a1;
            --orange: #fab387;
            --snippet-bg: #181825;
          }
          * { box-sizing: border-box; margin: 0; padding: 0; }
          body {
            font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', 'SF Mono', Menlo, monospace;
            background: var(--bg); color: var(--text);
            display: flex; justify-content: center; align-items: flex-start;
            min-height: 100vh; padding: 40px 16px;
          }
          .overlay {
            width: 600px; max-width: 100%;
            background: var(--surface);
            border-radius: 12px; overflow: hidden;
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
          }
          .input-bar {
            display: flex; align-items: center; gap: 8px;
            padding: 16px 18px;
            background: var(--snippet-bg);
            position: relative;
          }
          .face {
            font-family: 'SF Mono', Menlo, monospace;
            font-size: 16px; font-weight: 500;
            animation: facePulse 2s ease-in-out infinite;
          }
          @keyframes facePulse {
            0%, 100% { opacity: 1; transform: translateY(0); }
            50% { opacity: 0.7; transform: translateY(-2px); }
          }
          .query-text {
            font-size: 18px; color: var(--text);
            overflow: hidden; white-space: nowrap;
            animation: typing 1.2s steps(40, end);
          }
          @keyframes typing {
            from { width: 0; }
            to { width: 100%; }
          }
          .results-area { padding: 12px; }
          .results-header {
            display: flex; align-items: center; gap: 8px;
            padding: 4px 4px 8px;
            font-size: 12px; font-weight: 500; color: var(--muted);
          }
          .source-badge {
            font-size: 11px; font-weight: 600;
            padding: 2px 7px; border-radius: 4px;
            margin-left: auto;
            background: rgba(137,180,250,0.12); color: var(--accent);
          }
          .result-row {
            padding: 8px; margin-bottom: 4px;
            border-radius: 6px;
            animation: slideUp 0.35s ease-out forwards;
            opacity: 0;
          }
          .result-row:hover { background: rgba(255,255,255,0.04); }
          @keyframes slideUp {
            from { opacity: 0; transform: translateY(8px); }
            to { opacity: 1; transform: translateY(0); }
          }
          .result-header {
            display: flex; align-items: baseline; gap: 8px;
            margin-bottom: 6px;
          }
          .file-line { font-size: 13px; font-weight: 600; color: var(--text); }
          .result-source { font-size: 10px; color: var(--muted); margin-left: auto; }
          .result-score { font-size: 11px; font-weight: 500; }
          .score-high { color: var(--green); }
          .score-mid { color: var(--orange); }
          .score-low { color: var(--muted); }
          .result-snippet {
            font-size: 12px; font-family: 'SF Mono', Menlo, monospace;
            color: var(--muted); background: var(--snippet-bg);
            padding: 6px; border-radius: 4px;
            white-space: pre-wrap; word-break: break-all;
          }
          .struct-badge {
            display: inline-block; margin-top: 4px;
            font-size: 10px; font-weight: 500;
            color: #a0a0b8; background: rgba(255,255,255,0.04);
            padding: 2px 6px; border-radius: 3px;
          }
          .footer {
            text-align: center; padding: 16px;
            font-size: 11px; color: var(--muted);
            border-top: 1px solid rgba(255,255,255,0.05);
          }
          .footer a { color: var(--accent); text-decoration: none; }
        </style>
        </head>
        <body>
        <div class="overlay">
          <div class="input-bar">
            <span class="face">\(escapedFace)</span>
            <span class="query-text">\(queryText)</span>
          </div>
          <div class="results-area">
            <div class="results-header">
              \(escapedFace)
              \(n) result\(n == 1 ? "" : "s")
              <span class="source-badge">\(sourceLabel)</span>
            </div>
            \(resultRows)
          </div>
          <div class="footer">
            Compy Session · \(now) · <a href="https://github.com">Compy</a>
          </div>
        </div>
        </body>
        </html>
        """
    }

    /// 80% standard count, 20% personality phrasing.
    private var resultHeaderCopy: String {
        if !state.resultHeaderText.isEmpty {
            return state.resultHeaderText
        }
        let n = state.results.count
        return "\(n) result\(n == 1 ? "" : "s")"
    }

    /// Color-coded by backend tier — matches the tier-of-origin face system.
    private var sourceBadgeColor: Color {
        switch state.sourceLabel {
        case "freebuff", "ollama": return .green
        case "heuristic": return .blue
        case "graph": return .cyan
        case "trace": return .green
        case "git": return .indigo
        case "grep", "stub": return .teal
        default: return .orange
        }
    }

    // MARK: - Refactor Proposal View

    private var refactorProposalView: some View {
        VStack(spacing: 0) {
            faceView(size: 28)
                .padding(.top, 12)
                .padding(.bottom, 4)

            HStack {
                Text("\(state.refactorProposals.count) file\(state.refactorProposals.count == 1 ? "" : "s") would change")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(.primary)
                Spacer()
            }
            .padding(.horizontal, 12)
            .padding(.top, 8)

            ScrollView {
                LazyVStack(spacing: 4) {
                    ForEach(state.refactorProposals) { proposal in
                        HStack {
                            Image(systemName: "doc.text")
                                .font(.system(size: 12))
                                .foregroundColor(.secondary)
                            Text(proposal.file)
                                .font(.system(size: 13, design: .monospaced))
                                .foregroundColor(.primary)
                                .lineLimit(1)
                            Spacer()
                            Text("~\(proposal.changedLines) lines")
                                .font(.system(size: 11))
                                .foregroundColor(Color(NSColor.tertiaryLabelColor))
                        }
                        .padding(8)
                        .background(Color(NSColor.textBackgroundColor))
                        .cornerRadius(6)
                    }
                }
                .padding(12)
            }

            HStack(spacing: 20) {
                Button(action: rejectRefactor) {
                    HStack(spacing: 4) {
                        Image(systemName: "xmark.circle.fill")
                        Text("Reject (Esc)")
                    }
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(.red)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                    .background(Color.red.opacity(0.1))
                    .cornerRadius(6)
                }
                .buttonStyle(.plain)

                Button(action: confirmRefactor) {
                    HStack(spacing: 4) {
                        Image(systemName: "checkmark.circle.fill")
                        Text("Accept (Enter)")
                    }
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(.green)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                    .background(Color.green.opacity(0.1))
                    .cornerRadius(6)
                }
                .buttonStyle(.plain)
            }
            .padding(.bottom, 16)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func confirmRefactor() {
        guard let token = state.refactorToken else { return }
        state.text = "/confirm \(token)"
        submitQuery()
    }

    private func rejectRefactor() {
        withAnimation(.easeOut(duration: 0.2)) {
            state.refactorToken = nil
            state.refactorProposals = []
            state.text = ""
            state.phase = .empty
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

    /// Try to extract a workspace-switch directive from natural language.
    /// Patterns: "find X in /path", "find X in garden_warriors/",
    /// "search in ~/code/project for X".
    /// Returns (query, path) on success, nil if no workspace directive detected.
    private func tryNLWorkspaceSwitch(_ raw: String) -> (query: String, path: String)? {
        let range = NSRange(raw.startIndex..<raw.endIndex, in: raw)

        // Pattern 1: "... in /absolute/path..." or "... in ~/path..."
        // Must start with / or ~/ — avoids matching normal words.
        let absRe = try? NSRegularExpression(
            pattern: "\\bin\\s+((?:~/|/)[/\\w.+-]+)\\b", options: [.caseInsensitive]
        )
        // Pattern 2: "... in word-name/ ..." (relative dir, MUST end with /)
        let relRe = try? NSRegularExpression(
            pattern: "\\bin\\s+([\\w][\\w.-]*/)\\s*", options: [.caseInsensitive]
        )

        // Try absolute first (more reliable).
        for (re, isRel) in [(absRe, false), (relRe, true)] {
            guard let regex = re else { continue }
            guard let match = regex.firstMatch(in: raw, range: range),
                  match.numberOfRanges >= 2,
                  let pathRange = Range(match.range(at: 1), in: raw) else { continue }

            var pathStr = String(raw[pathRange])
            // Only strip trailing slash — preserving leading / for absolute paths.
            if pathStr.hasSuffix("/") { pathStr.removeLast() }

            let resolved: String
            if isRel {
                // Relative: resolve against parent of current workspace.
                let parent = state.workspaceRoot.map { URL(fileURLWithPath: $0).deletingLastPathComponent().path } ?? NSHomeDirectory()
                resolved = URL(fileURLWithPath: pathStr, relativeTo: URL(fileURLWithPath: parent)).path
            } else {
                let expanded = (pathStr as NSString).expandingTildeInPath
                resolved = expanded.hasPrefix("/") ? expanded : URL(fileURLWithPath: expanded, relativeTo: repoRoot).path
            }

            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: resolved, isDirectory: &isDir), isDir.boolValue else { continue }

            // Remove the "in <path>" clause from the query.
            let clauseRange = Range(match.range(at: 0), in: raw)!
            var query = raw
            query.removeSubrange(clauseRange)
            let cleaned = query.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
                .trimmingCharacters(in: .whitespaces)
            return (query: cleaned.isEmpty ? raw : cleaned, path: resolved)
        }
        return nil
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
        guard state.phase != .processing, state.phase != .refactorProposal else { return }

        // ── Built-in commands ───────────────────────────────────
        // /workspace <path> — switch the search directory.
        if state.text.hasPrefix("/workspace ") {
            let raw = state.text.dropFirst(11).trimmingCharacters(in: .whitespaces)
            guard !raw.isEmpty else {
                withAnimation(.easeOut(duration: 0.15)) {
                    state.toastMessage = "✗ Usage: /workspace ~/path/to/project"
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                    withAnimation(.easeOut(duration: 0.2)) { state.toastMessage = nil }
                }
                state.text = ""
                return
            }
            let expanded = (raw as NSString).expandingTildeInPath
            let path = expanded.hasPrefix("/") ? expanded : URL(fileURLWithPath: expanded, relativeTo: repoRoot).path
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: path, isDirectory: &isDir), isDir.boolValue else {
                withAnimation(.easeOut(duration: 0.15)) {
                    state.toastMessage = "✗ Not found: \(raw)"
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                    withAnimation(.easeOut(duration: 0.2)) { state.toastMessage = nil }
                }
                state.text = ""
                return
            }
            state.workspaceRoot = path
            state.selectionFile = nil
            state.selectionLine = nil
            state.selectionText = ""
            let name = URL(fileURLWithPath: path).lastPathComponent
            withAnimation(.easeOut(duration: 0.15)) {
                state.text = ""
                state.toastMessage = "✓ Now searching: \(name)/"
            }
            hapticSubmit()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                withAnimation(.easeOut(duration: 0.2)) { state.toastMessage = nil }
            }
            return
        }

        // ── Natural-language workspace switching ───────────────
        // "find X in /path" or "search project-name for Y"
        if let switched = tryNLWorkspaceSwitch(state.text) {
            state.workspaceRoot = switched.path
            state.selectionFile = nil
            state.selectionLine = nil
            state.selectionText = ""
            let name = URL(fileURLWithPath: switched.path).lastPathComponent
            withAnimation(.easeOut(duration: 0.15)) {
                state.text = switched.query  // the query part without the workspace clause
                state.toastMessage = "✓ Switched to: \(name)/"
            }
            hapticSubmit()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
                withAnimation(.easeOut(duration: 0.2)) { state.toastMessage = nil }
            }
            return
        }
        // ────────────────────────────────────────────────────────

        let question = state.text

        hapticSubmit()
        state.stopIdleFloat()
        state.startProcessingPulse()

        // Generate stable progress messages BEFORE the phase change so they're
        // ready on the first .processing render — avoids a one-frame flash of
        // the fallback "Working…" message.
        currentProgressMessages = Self.buildProgressMessages()

        withAnimation(.easeOut(duration: 0.15)) {
            state.phase = .processing
            // Preserve current results dimmed (session memory) while new query runs.
            // Only overwrite previousResults if there are results to preserve —
            // don't clobber results that the clear button already preserved.
            if !state.results.isEmpty {
                state.previousResults = state.results
            }
            state.results = []
            state.reasonText = nil
            state.resultHeaderText = ""
        }

        // Freeze the intent guess before text is cleared — used by processing face.
        state.submittedIntentGuess = state.captureIntentGuess()

        DispatchQueue.global(qos: .userInitiated).async {
            // Resolve the active workspace — the directory ripgrep searches.
            // Priority: extension workspaceRoot > derived from selection file > COMPY_ROOT.
            let activeWorkspace = Self.resolveActiveWorkspace(
                extRoot: state.workspaceRoot,
                selectionFile: state.selectionFile,
                fallbackRoot: repoRoot.path
            )
            // Always send a Selection so the daemon receives the correct
            // workspace_root — even when no text/file was selected.
            //
            // Sync the workspace indicator so the user sees what was actually searched.
            DispatchQueue.main.async {
                state.workspaceRoot = activeWorkspace
            }
            let sel = Selection(
                text: state.selectionText,
                file: state.selectionFile,
                line: state.selectionLine,
                workspaceRoot: activeWorkspace
            )

            // Layer 0: pass previous turn's results as session context for follow-ups.
            let sessionCtx: [String]? = state.previousResults.isEmpty ? nil :
                state.previousResults.prefix(3).map { "\($0.file):\($0.line): \($0.snippet)" }
            let request = QueryRequest(question: question, selection: sel, stream: true, sessionContext: sessionCtx)
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
                var streamedCount: Int?
                let done = DispatchSemaphore(value: 0)
                stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
                    let chunk = handle.availableData
                    if chunk.isEmpty {
                        handle.readabilityHandler = nil
                        done.signal()
                    } else {
                        lock.lock()
                        allOutput.append(chunk)
                        // Try to parse the first complete JSON line as stream event.
                        if streamedCount == nil, let text = String(data: allOutput, encoding: .utf8) {
                            if let nl = text.firstIndex(of: "\n") {
                                let firstLine = String(text[..<nl])
                                if let lineData = firstLine.data(using: .utf8),
                                   let event = try? compyDecoder.decode(StreamEvent.self, from: lineData),
                                   event.stream == "candidates" {
                                    streamedCount = event.count
                                    _debugLog("streamed \(event.count) candidates to overlay")
                                    DispatchQueue.main.async {
                                        // Haptic tap — immediate tactile feedback that candidates arrived.
                                        NSHapticFeedbackManager.defaultPerformer.perform(.alignment, performanceTime: .default)
                                        // Show dimmed grep candidates immediately while reasoner runs.
                                        withAnimation(.easeOut(duration: 0.15)) {
                                            state.results = event.hits
                                            state.resultHeaderText = "Ranking \(event.count) candidates…"
                                        }
                                        // Drive the compact input-bar progress badge.
                                        inputBarIsStreaming = true
                                        inputBarStreamCount = event.count
                                    }
                                }
                            }
                        }
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

                // Decode the last JSON line as the final QueryResult.
                let text = String(data: outData, encoding: .utf8) ?? ""
                let lines = text.components(separatedBy: .newlines).filter { !$0.isEmpty }
                let finalData = lines.last?.data(using: .utf8) ?? outData

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

                if let result = try? compyDecoder.decode(QueryResult.self, from: finalData) {
                    DispatchQueue.main.async {
                        state.stopProcessingPulse()
                        // Clear streaming indicators now that final results are here.
                        inputBarIsStreaming = false
                        inputBarStreamCount = 0
                        let n = result.hits.count
                        // 20% chance of personality-flavored result header
                        if n > 0 && CompyMessagePool.shouldUsePersonality() {
                            let template = CompyMessagePool.pick(from: CompyMessagePool.resultHeaders, category: "resultHeaders")
                            if template.contains("%@") {
                                let source = state.sourceLabel.capitalized
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
                        state.lastIntent = result.intent  // for intent-reflecting face
                        state.previousResults = []  // clear old results on success
                        state.lastQuestion = question  // capture for session export
                        state.text = ""  // clear text so user can type follow-up immediately
                        withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                            state.results = result.hits
                            state.reasonText = result.reason
                            if result.hits.isEmpty {
                                state.phase = .noMatch
                            } else {
                                if let proposals = result.refactorProposals, !proposals.isEmpty {
                                state.refactorToken = result.refactorToken
                                state.refactorProposals = proposals
                                state.phase = .refactorProposal
                            } else {
                                state.phase = result.degraded ? .degraded : .results
                            }
                            }
                        }
                        if result.hits.isEmpty {
                            if result.intent == "format" && !result.degraded {
                                // Format succeeded — show toast and reset.
                                state.toastMessage = "✓ Formatted"
                                state.text = ""
                                state.phase = .empty
                                hapticSuccess()
                                DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                                    withAnimation(.easeOut(duration: 0.2)) { state.toastMessage = nil }
                                }
                            } else {
                                hapticNoMatch()
                                state.noMatchSuggestions = result.suggestions
                                state.noMatchHint = CompyMessagePool.pick(from: CompyMessagePool.noMatchHints, category: "noMatchHints")
                            }
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
    /// Shimmer sweep position — animates from -200 to 600 for dimmed intermediate rows.
    @State private var shimmerOffset: CGFloat = -200
    /// True once the shimmer animation has started.
    @State private var shimmerStarted = false

    /// Whether this row is an intermediate (streamed) candidate awaiting ranking.
    private var isIntermediate: Bool { hit.score == 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("\(hit.file):\(hit.line)")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(.primary)
                    .lineLimit(1)
                Spacer()
                if !isIntermediate {
                    Text("via \(hit.source)")
                        .font(.system(size: 10))
                        .foregroundColor(Color(NSColor.tertiaryLabelColor))
                    Text("\(Int(hit.score * 100))%")
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundColor(scoreColor(hit.score))
                }
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

            // Structural context badge: "Called by: login_handler, auth_mw"
            // Sourced from Graphify after ranking — shows callers/importers.
            if let ctx = hit.structuralContext, !ctx.isEmpty {
                HStack(spacing: 4) {
                    Image(systemName: "arrow.triangle.branch")
                        .font(.system(size: 9))
                    Text(ctx)
                        .font(.system(size: 10, weight: .medium))
                        .lineLimit(1)
                }
                .foregroundColor(Color(NSColor.tertiaryLabelColor))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(Color(NSColor.quaternaryLabelColor))
                .cornerRadius(3)
            }
        }
        .padding(8)
        .background(
            ZStack {
                RoundedRectangle(cornerRadius: 6)
                    .fill(isHovered
                        ? Color(NSColor.quaternaryLabelColor)
                        : Color.clear)
                // Shimmer sweep — a ghostly highlight gliding across dimmed rows.
                if isIntermediate && appeared && shimmerStarted {
                    GeometryReader { geo in
                        Rectangle()
                            .fill(
                                LinearGradient(
                                    gradient: Gradient(stops: [
                                        .init(color: Color.white.opacity(0), location: 0),
                                        .init(color: Color.white.opacity(0.04), location: 0.3),
                                        .init(color: Color.white.opacity(0.10), location: 0.5),
                                        .init(color: Color.white.opacity(0.04), location: 0.7),
                                        .init(color: Color.white.opacity(0), location: 1),
                                    ]),
                                    startPoint: .leading,
                                    endPoint: .trailing
                                )
                            )
                            .frame(width: geo.size.width * 0.5)
                            .offset(x: shimmerOffset)
                    }
                }
            }
        )
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .opacity(appeared ? (isIntermediate ? 0.45 : 1) : 0)
        .offset(y: appeared ? 0 : 8)
        .onAppear {
            withAnimation(.spring(response: 0.35, dampingFraction: 0.8).delay(Double(index) * 0.04)) {
                appeared = true
            }
            // Start shimmer sweep for intermediate candidates — a gentle,
            // repeating left-to-right highlight that signals "still ranking."
            // Staggered per row (0.12s × index) so the sweep reads as a
            // cascading wave across the result list, not a flash sync.
            if isIntermediate {
                let staggerDelay = Double(index) * 0.12
                DispatchQueue.main.asyncAfter(deadline: .now() + staggerDelay) { [self] in
                    guard self.isIntermediate else { return }
                    shimmerStarted = true
                    withAnimation(.linear(duration: 1.8).repeatForever(autoreverses: false)) {
                        shimmerOffset = 600
                    }
                }
            }
        }
        .onChange(of: hit.score) { _, newScore in
            // When the daemon returns ranked results (score > 0), the shimmer
            // naturally stops because isIntermediate becomes false and the
            // offset animation target changes. SwiftUI's spring replace animation
            // handles the transition to full opacity.
            _ = newScore
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
            OverlayController.shared.hide(); EditorOpener.open(file: hit.file, line: hit.line)
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
