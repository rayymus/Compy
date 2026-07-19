/**
 * Compy Companion — Antigravity/VS Code extension
 *
 * Contract per `.agent/SPEC.md` §4:
 *   - On Cmd+Shift+Space hotkey, captures the active editor's selection + file/line +
 *     workspace root, and writes a JSON envelope to both a UNIX socket and a JSON file.
 *   - The Swift overlay or the Compy daemon reads this envelope for selection grounding.
 *
 * §2-4 (claude-response5): LSP bridge — the extension now also listens on a
 * second socket (/tmp/compy-lsp.sock) for LSP query requests from the Python
 * daemon.  The daemon sends a JSON request and the extension executes the
 * corresponding VS Code LSP command (definition, references, hover) and returns
 * results.  This lets the daemon enrich search/explain/refactor with live
 * semantic data from the editor's language server.
 *
 * §4 (claude-response5): Refactor propose/confirm — the extension can render
 * inline text decorations in the active buffer for a non-mutating preview,
 * and apply the edit through the editor's document-edit API for native undo.
 */

import * as fs from "node:fs";
import * as net from "node:net";
import * as vscode from "vscode";

// ── Selection capture sockets ──────────────────────────────────────────
const SOCKET_PATH =
  process.env.COMPY_SOCKET ?? "/tmp/compy-selection.sock";
const FILE_PATH =
  process.env.COMPY_SELECTION_FILE ?? "/tmp/compy-selection.json";

// ── LSP bridge socket (§2, claude-response5) ────────────────────────────
const LSP_SOCKET_PATH =
  process.env.COMPY_LSP_SOCKET ?? "/tmp/compy-lsp.sock";

interface SelectionEnvelope {
  file: string;
  line: number;
  workspaceRoot: string;
  selectedText: string;
  ts: number;
}

// ── LSP request/response types (§2) ────────────────────────────────────
interface LspRequest {
  type: "definition" | "references" | "hover" | "rename";
  symbol: string;
  file?: string;
  line?: number;
  // rename-specific
  newName?: string;
}

interface LspResponse {
  type: string;
  ok: boolean;
  error?: string;
  results?: LspResult[];
}

interface LspResult {
  file: string;
  line: number;
  snippet: string;
}

// ── Refactor propose types (§4) ────────────────────────────────────────
interface RefactorProposeRequest {
  type: "propose" | "apply" | "dismiss";
  // propose: show inline decoration
  symbol?: string;
  newText?: string;
  file?: string;
  line?: number;
  // apply: commit the pending decoration to the live buffer
  // dismiss: remove the decoration without touching the buffer
}

interface RefactorProposeResponse {
  ok: boolean;
  error?: string;
}

// Track the pending refactor decoration so we can dismiss it.
let _pendingDecoration: vscode.TextEditorDecorationType | null = null;
let _pendingEditor: vscode.TextEditor | null = null;
let _pendingRanges: vscode.Range[] | null = null;

export function activate(context: vscode.ExtensionContext): void {
  // ── Selection capture (existing) ───────────────────────────────────
  const hotkeyDisposable = vscode.commands.registerCommand(
    "compy.companion.hotkey",
    sendSelectionEnvelope,
  );
  context.subscriptions.push(hotkeyDisposable);

  sendSelectionEnvelope();

  const editorDisposable = vscode.window.onDidChangeActiveTextEditor(
    () => sendSelectionEnvelope(),
  );
  context.subscriptions.push(editorDisposable);

  const wsDisposable = vscode.workspace.onDidChangeWorkspaceFolders(
    () => sendSelectionEnvelope(),
  );
  context.subscriptions.push(wsDisposable);

  const focusDisposable = vscode.window.onDidChangeWindowState((e) => {
    if (e.focused) {
      sendSelectionEnvelope();
    }
  });
  context.subscriptions.push(focusDisposable);

  // ── LSP bridge — listen for daemon queries (§2, claude-response5) ──
  startLspBridge(context);
}

// ── LSP bridge server (§2) ──────────────────────────────────────────────
function startLspBridge(context: vscode.ExtensionContext): void {
  // Clean up stale socket file.
  try { fs.unlinkSync(LSP_SOCKET_PATH); } catch { /* ok */ }

  const server = net.createServer((conn) => {
    let data = "";
    conn.on("data", (chunk: Buffer) => {
      data += chunk.toString("utf-8");
      // Guard: 64KB cap
      if (data.length > 64 * 1024) {
        conn.destroy();
        return;
      }
    });
    conn.on("end", () => {
      try {
        const req: LspRequest | RefactorProposeRequest = JSON.parse(data);
        if (req.type === "propose" || req.type === "apply" || req.type === "dismiss") {
          handleRefactorPropose(req as RefactorProposeRequest).then((resp) => {
            conn.write(JSON.stringify(resp));
            conn.end();
          });
        } else {
          handleLspRequest(req as LspRequest).then((resp) => {
            conn.write(JSON.stringify(resp));
            conn.end();
          });
        }
      } catch {
        const resp: LspResponse = { type: "error", ok: false, error: "Invalid JSON" };
        conn.write(JSON.stringify(resp));
        conn.end();
      }
    });
    conn.on("error", () => {
      conn.destroy();
    });
  });

  server.listen(LSP_SOCKET_PATH, () => {
    try { fs.chmodSync(LSP_SOCKET_PATH, 0o666); } catch { /* ok */ }
  });

  context.subscriptions.push({ dispose: () => server.close() });
}

// ── LSP query handler (§2) ──────────────────────────────────────────────
async function handleLspRequest(req: LspRequest): Promise<LspResponse> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    return { type: req.type, ok: false, error: "No active editor" };
  }

  try {
    switch (req.type) {
      case "definition": {
        const uri = editor.document.uri;
        const pos = req.line != null
          ? new vscode.Position(req.line - 1, 0)
          : editor.selection.active;
        const locs = await vscode.commands.executeCommand<vscode.Location[]>(
          "vscode.executeDefinitionProvider", uri, pos
        ) ?? [];
        const results: LspResult[] = [];
        for (const loc of locs) {
          results.push({
            file: loc.uri.fsPath,
            line: loc.range.start.line + 1,
            snippet: await readSnippet(loc.uri, loc.range.start.line),
          });
        }
        return { type: req.type, ok: true, results };
      }
      case "references": {
        const uri = editor.document.uri;
        const pos = req.line != null
          ? new vscode.Position(req.line - 1, 0)
          : editor.selection.active;
        const locs = await vscode.commands.executeCommand<vscode.Location[]>(
          "vscode.executeReferenceProvider", uri, pos
        ) ?? [];
        const results: LspResult[] = [];
        for (const loc of locs) {
          results.push({
            file: loc.uri.fsPath,
            line: loc.range.start.line + 1,
            snippet: await readSnippet(loc.uri, loc.range.start.line),
          });
        }
        return { type: req.type, ok: true, results };
      }
      case "hover": {
        const uri = editor.document.uri;
        const pos = req.line != null
          ? new vscode.Position(req.line - 1, 0)
          : editor.selection.active;
        const hovers = await vscode.commands.executeCommand<vscode.Hover[]>(
          "vscode.executeHoverProvider", uri, pos
        ) ?? [];
        const results: LspResult[] = [];
        for (const h of hovers) {
          const text = h.contents.map((c) =>
            typeof c === "string" ? c : (c as { language: string; value: string }).value
          ).join("\n");
          if (text) {
            results.push({
              file: editor.document.uri.fsPath,
              line: pos.line + 1,
              snippet: text.slice(0, 300),
            });
          }
        }
        return { type: req.type, ok: true, results };
      }
      case "rename": {
        if (!req.newName) {
          return { type: req.type, ok: false, error: "Missing newName for rename" };
        }
        const uri = editor.document.uri;
        const pos = req.line != null
          ? new vscode.Position(req.line - 1, 0)
          : editor.selection.active;
        const edit = await vscode.commands.executeCommand<vscode.WorkspaceEdit>(
          "vscode.executeDocumentRenameProvider", uri, pos, req.newName
        );
        if (!edit || edit.size === 0) {
          return { type: req.type, ok: false, error: "No rename edits returned" };
        }
        const results: LspResult[] = [];
        for (const [uriStr, edits] of edit.entries()) {
          for (const e of edits) {
            results.push({
              file: uriStr.fsPath,
              line: e.range.start.line + 1,
              snippet: `${e.newText} (was: ${readSnippetSync(uriStr, e.range.start.line)})`,
            });
          }
        }
        return { type: req.type, ok: true, results };
      }
      default:
        return { type: req.type, ok: false, error: `Unknown type: ${req.type}` };
    }
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    return { type: req.type, ok: false, error: msg };
  }
}

// ── Refactor propose/confirm handler (§4, claude-response5) ────────────
async function handleRefactorPropose(
  req: RefactorProposeRequest
): Promise<RefactorProposeResponse> {
  switch (req.type) {
    case "propose": {
      // Render an inline decoration in the active buffer showing the proposed edit.
      // Non-mutating — nothing touches the document's undo history.
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        return { ok: false, error: "No active editor" };
      }
      // Dismiss any previous decoration.
      dismissPendingDecoration();

      const line = (req.line ?? 1) - 1;
      const range = new vscode.Range(line, 0, line, editor.document.lineAt(line).text.length);

      // Create a decoration type that shows ghost text after the line.
      _pendingDecoration = vscode.window.createTextEditorDecorationType({
        after: {
          contentText: `  →  ${req.newText ?? ""}`,
          color: new vscode.ThemeColor("editorCodeLens.foreground"),
          fontStyle: "italic",
          margin: "0 0 0 20px",
        },
        backgroundColor: new vscode.ThemeColor("editor.findMatchHighlightBackground"),
        border: "1px dashed",
        borderColor: new vscode.ThemeColor("editorCursor.foreground"),
      });

      _pendingEditor = editor;
      _pendingRanges = [range];
      editor.setDecorations(_pendingDecoration, [range]);

      return { ok: true };
    }

    case "apply": {
      // Commit the proposed change to the live buffer via editor.edit()
      // → real undo-stack entry, indistinguishable from manual typing (§4).
      if (!_pendingEditor || !_pendingDecoration) {
        return { ok: false, error: "No pending proposal to apply" };
      }
      try {
        const ranges = _pendingRanges;
        const ok = await _pendingEditor.edit((editBuilder) => {
          if (ranges && req.newText) {
            for (const range of ranges) {
              editBuilder.replace(range, req.newText);
            }
          }
        });
        dismissPendingDecoration();
        return ok ? { ok: true } : { ok: false, error: "Edit failed" };
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        return { ok: false, error: msg };
      }
    }

    case "dismiss": {
      dismissPendingDecoration();
      return { ok: true };
    }

    default:
      return { ok: false, error: `Unknown type: ${(req as any).type}` };
  }
}

function dismissPendingDecoration(): void {
  if (_pendingDecoration) {
    _pendingDecoration.dispose();
    _pendingDecoration = null;
  }
  _pendingEditor = null;
  _pendingRanges = null;
}

// ── Helpers ─────────────────────────────────────────────────────────────
async function readSnippet(
  uri: vscode.Uri,
  line: number,
): Promise<string> {
  try {
    const doc = await vscode.workspace.openTextDocument(uri);
    if (line < doc.lineCount) {
      return doc.lineAt(line).text.slice(0, 200);
    }
  } catch {
    // File not open — read from disk.
  }
  try {
    const content = fs.readFileSync(uri.fsPath, "utf-8");
    const lines = content.split("\n");
    if (line < lines.length) {
      return lines[line].slice(0, 200);
    }
  } catch {
    // Can't read — return empty.
  }
  return "";
}

function readSnippetSync(
  uri: vscode.Uri,
  line: number,
): string {
  try {
    const content = fs.readFileSync(uri.fsPath, "utf-8");
    const lines = content.split("\n");
    if (line < lines.length) {
      return lines[line].slice(0, 200);
    }
  } catch {
    // ok
  }
  return "";
}

// ── Selection envelope (existing) ────────────────────────────────────────
function sendSelectionEnvelope(): void {
  const editor = vscode.window.activeTextEditor;
  const wsFolder =
    vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? "/";

  const envelope: SelectionEnvelope = {
    file: editor?.document.uri.fsPath ?? "",
    line: editor?.selection.start.line ?? 0,
    workspaceRoot: wsFolder,
    selectedText: editor?.document.getText(editor?.selection) ?? "",
    ts: Math.floor(Date.now() / 1000),
  };

  const payload = JSON.stringify(envelope);

  try {
    fs.writeFileSync(FILE_PATH, payload, "utf-8");
  } catch {
    // Permission issue — silently skip.
  }

  const client = net.createConnection(SOCKET_PATH);
  client.on("connect", () => {
    client.write(payload);
    client.end();
  });
  client.on("error", () => {
    client.destroy();
  });
}

export function deactivate(): void {
  dismissPendingDecoration();
}
