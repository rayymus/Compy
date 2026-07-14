# Compy Companion — VS Code / Antigravity Extension

Companion extension that grounds the Compy overlay with file/line/workspace context that
clipboard-swap (§4 of the spec) alone cannot reliably capture.

## Contract

On the Compy hotkey (Cmd+Shift+Space — same key the Swift overlay registers), this extension:

1. Reads `vscode.window.activeTextEditor`'s selection (text + start line).
2. Reads `vscode.workspace.workspaceFolders[0].uri.fsPath` for the workspace root.
3. Writes a JSON envelope to a UNIX socket at `/tmp/compy-selection.sock` AND a JSON file at `/tmp/compy-selection.json`:

```json
{
  "file": "/path/to/file.py",
  "line": 42,
  "workspaceRoot": "/path/to/repo",
  "selectedText": "def get_ability(self):\n    return self._ability",
  "ts": 1700000000000
}
```

The Swift overlay's `HotkeyManager.swift` reads the JSON file; the `socket_listener.py`
daemon reads the socket and caches to file for redundancy.

## Build

```sh
cd compy/extension
npm install
npm run build   # produces out/extension.js
```

Load in Antigravity via the "Run and Debug → Launch Extension" flow, or install to
`~/.vscode/extensions/compy-companion`.

## Keybinding

`package.json` registers `cmd+shift+space` when `editorTextFocus`. This fires the
`compy.companion.hotkey` command which writes the selection envelope.

## Status

- ✅ Compiles (TypeScript → JS)
- ✅ Keybinding wired (`cmd+shift+space` when `editorTextFocus`)
- ✅ Writes to socket AND JSON file (redundancy)
- ⚠️ Multi-workspace selection not yet wired (picks first workspace folder)
