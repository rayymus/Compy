import Foundation

// MARK: - JSON Coding Helpers

/// Shared encoder for Python daemon compat — only Selection needs snake_case keys.
let compyEncoder: JSONEncoder = {
    let e = JSONEncoder()
    return e
}()

let compyDecoder: JSONDecoder = {
    let d = JSONDecoder()
    return d
}()

// MARK: - Daemon Data Contracts

/// Mirrors compy.daemon.models.RankedHit — what the daemon returns in QueryResult.hits.
struct RankedHit: Codable, Identifiable {
    var id: String { "\(file):\(line)" }
    let file: String
    let line: Int
    let snippet: String
    let score: Double
    let source: String
    /// Optional structural badge from Graphify: "Called by: login_handler, auth_mw"
    let structuralContext: String?

    enum CodingKeys: String, CodingKey {
        case file, line, snippet, score, source
        case structuralContext = "structural_context"
    }
}

/// Mirrors compy.daemon.models.QueryRequest — what the daemon expects on stdin.
struct QueryRequest: Codable {
    let question: String
    let selection: Selection?
    let stream: Bool  // when true, daemon emits intermediate candidates before ranking
}

/// Selection with explicit CodingKeys for `workspace_root` (daemon uses snake_case).
struct Selection: Codable {
    let text: String
    let file: String?
    let line: Int?
    let workspaceRoot: String?

    enum CodingKeys: String, CodingKey {
        case text, file, line
        case workspaceRoot = "workspace_root"
    }
}

/// Mirrors compy.daemon.models.QueryResult — what the daemon returns on stdout.
struct QueryResult: Codable {
    let intent: String
    let hits: [RankedHit]
    let degraded: Bool
    let reason: String?
    let suggestions: [String]?  // "did you mean X?" on no-match
}

/// Intermediate streaming event: grep candidates emitted before ranking completes.
/// The daemon writes this as the first JSON line when stream=true is set.
struct StreamEvent: Codable {
    let stream: String
    let hits: [RankedHit]
    let count: Int
}

/// Envelope from the VS Code companion extension, written to /tmp/compy-selection.json.
/// Decoded via JSONSerialization in HotkeyManager (not Codable), so no CodingKeys needed.
struct SelectionEnvelope: Codable {
    let file: String
    let line: Int
    let workspaceRoot: String
    let selectedText: String
    let ts: Int
}
