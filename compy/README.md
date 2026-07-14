# Compy

> A global-hotkey code-search overlay for macOS. Press **Cmd+Shift+Space**, ask a question about your codebase in voice or text, and jump directly to the answer. Zero cost. Fully offline-capable.

---

## What it does

Compy is a Spotlight-like popup that answers codebase questions without leaving your editor:

- **"Where else is `get_ability` used?"** — finds every reference across the repo
- **"What calls `handle_request`?"** — traces callers through the call graph
- **"Where's the function that validates tokens?"** — fuzzy semantic search when you can't remember the exact name
- **"Who added this null check?"** — git blame and commit history

Every result is clickable — jumps directly to `file:line` in your editor.

---

## How it works

```
Your question (voice or text)
        │
        ▼
  Parse → classify intent (reference, definition, fuzzy, history...)
        │
        ▼
  Grep / Graph / Git  → find candidates (ripgrep, tree-sitter graph, git log)
        │
        ▼
  Reason → rank results (Freebuff → Ollama → heuristic → stub, cascading fallback)
        │
        ▼
  Ranked results → click to jump to file:line in editor
```

Every query runs through a 4-tier reasoner chain. If the strongest backend is unavailable, the next one takes over automatically — you always get an answer, never a hard error.

Pipeline: **parse → grep → reason**, six backends (ripgrep, Graphify, Git history, Freebuff, Ollama, heuristic), seven intent types.

---

## Quick start

```sh
# Terminal 1 — start Ollama (optional, for LLM-ranked results)
./compy.sh ollama-start

# Terminal 2 — launch the overlay
./compy.sh overlay
```

Press **Cmd+Shift+Space**. Type a question and press Enter. Click any result to jump.

For editor selection capture (the overlay knows which file and line you're on), install the companion VS Code extension:

```sh
./compy.sh build
```

---

## Subcommands

| Command | What it does |
|---------|-------------|
| `./compy.sh overlay` | Build and launch the Swift/SwiftUI overlay |
| `./compy.sh build` | Run all 81 tests + compile extension + build overlay |
| `./compy.sh test` | Run daemon tests only |
| `./compy.sh listen` | Start UNIX socket listener (for extension selection) |
| `./compy.sh query` | Pipe a JSON query into the daemon |
| `./compy.sh ollama-start` | Start Ollama in background |
| `./compy.sh ollama-stop` | Stop the Ollama server |
| `./compy.sh ollama-status` | Check Ollama status and loaded models |
| `./compy.sh stt-test` | Record 3s from mic and transcribe (test STT) |

---

## Architecture

Three vertical slices, all shipping:

| Slice | Tech | Purpose |
|-------|------|---------|
| **Daemon** | Python | Parse → grep → reason pipeline. 81 tests, zero dependencies beyond stdlib + tree-sitter + networkx. |
| **Overlay** | Swift/SwiftUI | Native macOS panel. Global hotkey, live STT, results display, jump-to-editor. Compiles to ~650KB Mach-O arm64 binary. |
| **Extension** | TypeScript (VS Code API) | Selection capture: writes `{file, line, workspaceRoot, selectedText}` to the overlay on hotkey trigger. |

### Memory systems (persistent codebase understanding)

- **Graphify** — tree-sitter → NetworkX code graph. Answers relational queries: "what calls this?", "what does this import?", "who subclasses X?". Persisted to `~/.compy/`.
- **Git history** — `git log --grep` + `git blame`. Answers historical queries: "who added this?", "what commit mentions X?", "why does this check exist?".

---

## Requirements

- macOS on Apple Silicon (M-series)
- Python 3.11+ with `tree-sitter`, `tree-sitter-python`, `networkx`
- Swift toolchain (Xcode or Command Line Tools)
- [Ollama](https://ollama.com) — optional, for LLM-ranked results
- [whisper-cpp](https://github.com/ggerganov/whisper.cpp) — optional, for voice input

No API keys. No subscriptions. No network required (Ollama + STT run locally).

---

## Docs

| File | Purpose |
|------|---------|
| [`.agent/SPEC.md`](.agent/SPEC.md) | Authoritative spec — architecture, data contracts, design rationale |
| [`.agent/DEVLOG.md`](.agent/DEVLOG.md) | Build-phase decisions across all sessions |
| [`.agent/UPDATES.md`](.agent/UPDATES.md) | Session-by-session changelog |
| [`compy/swift/README.md`](compy/swift/README.md) | Overlay build and architecture |
| [`compy/extension/README.md`](compy/extension/README.md) | Extension build and socket contract |
