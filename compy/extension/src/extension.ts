/**
 * Compy Companion — Antigravity/VS Code extension
 *
 * Contract per `.agent/SPEC.md` §4:
 *   - On Cmd+Shift+Space hotkey, captures the active editor's selection + file/line +
 *     workspace root, and writes a JSON envelope to both a UNIX socket and a JSON file.
 *   - The Swift overlay or the Compy daemon reads this envelope for selection grounding.
 */

import * as fs from "node:fs";
import * as net from "node:net";
import * as vscode from "vscode";

const SOCKET_PATH =
  process.env.COMPY_SOCKET ?? "/tmp/compy-selection.sock";
const FILE_PATH =
  process.env.COMPY_SELECTION_FILE ?? "/tmp/compy-selection.json";

interface SelectionEnvelope {
  file: string;
  line: number;
  workspaceRoot: string;
  selectedText: string;
  ts: number;
}

export function activate(context: vscode.ExtensionContext): void {
  const disposable = vscode.commands.registerCommand(
    "compy.companion.hotkey",
    sendSelectionEnvelope,
  );
  context.subscriptions.push(disposable);
}

function sendSelectionEnvelope(): void {
  const editor = vscode.window.activeTextEditor;
  const wsFolder =
    vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? "/";

  const envelope: SelectionEnvelope = {
    file: editor?.document.uri.fsPath ?? "",
    line: editor?.selection.start.line ?? 0,
    workspaceRoot: wsFolder,
    selectedText: editor?.document.getText(editor?.selection) ?? "",
    ts: Date.now(),
  };

  const payload = JSON.stringify(envelope);

  // Primary: write to JSON file — always works, no listener needed.
  try {
    fs.writeFileSync(FILE_PATH, payload, "utf-8");
  } catch {
    // Permission issue, read-only filesystem — silently skip.
  }

  // Secondary: also write to socket if the daemon listener is running.
  const client = net.createConnection(SOCKET_PATH);
  client.on("connect", () => {
    client.write(payload);
    client.end();
  });
  client.on("error", () => {
    // No listener — that's fine, the file write already worked.
  });
}

export function deactivate(): void {}
