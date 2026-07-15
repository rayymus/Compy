# Compy

> A global-hotkey code-search overlay for macOS. Press **Cmd+Shift+Space**, ask a question about your codebase in voice or text, and jump directly to the answer. Zero cost. Fully offline-capable.

---

## What it does

Compy is a Spotlight-like popup that answers codebase questions without leaving your editor:

- **"Where else is `get_ability` used?"** — finds every reference across the repo
- **"What calls `handle_request`?"** — traces callers through the call graph
- **"Where's the function that validates tokens?"** — fuzzy semantic search
- **"Who added this null check?"** — git blame and commit history
- **"What breaks if I change this?"** — blast radius via call graph
- **"Paste a stack trace"** — jumps straight to each frame's source

Every result is clickable — jumps directly to `file:line` in your editor.

Compy has a personality: an ASCII face-state mascot that blinks, winks, and
darts its eyes while searching, then morphs smoothly between expressions as the
pipeline progresses (idle → thinking → found → confused). Not a chatbot — a
status indicator wearing a mascot costume.

---

## Install & run

### Prerequisites
- **macOS** on Apple Silicon (M-series)
- **Python 3.11+** with `tree-sitter`, `tree-sitter-python`, `networkx`
- **Swift toolchain** (Xcode or Command Line Tools)
- **Ollama** — optional, for LLM-ranked results (`brew install ollama`)
- **whisper-cpp** — for voice input (`brew install whisper-cpp`)

```sh
# 1. Install Python deps
pip install tree-sitter tree-sitter-python networkx

# 2. Install system tools (optional but recommended)
brew install ollama whisper-cpp ffmpeg

# 3. Pull the Ollama model (for ranked results)
ollama pull qwen2.5-coder:1.5b
```

### Launch

```sh
# Terminal 1 — start Ollama
./compy.sh ollama-start

# Terminal 2 — launch the overlay
./compy.sh overlay
```

Press **Cmd+Shift+Space** to open the overlay. Type a question and press Enter. Click any result to jump.

For editor selection capture (so the overlay knows your current file, line, and project), install the companion VS Code extension:

```sh
./compy.sh build
```

Then load `compy/extension/` as an unpacked extension in VS Code / Antigravity IDE.

---

## Testing

After making changes, verify everything works:

```sh
# 1. Run the daemon test suite (102 tests)
./compy.sh test
# Expected: 102 passed, 1 skipped, 0 failed

# 2. Build the Swift overlay (catches compile errors)
./compy.sh overlay
# Expected: "Build complete!" — binary at compy/swift/.build/debug/Compy

# 3. Full build: tests + extension + overlay
./compy.sh build
# Expected: all three pass

# 4. Test STT (records 3s from mic, transcribes via whisper.cpp)
./compy.sh stt-test
# Expected: JSON output with transcribed text
# {"text": "hello world", "success": true}

# 5. Manual integration test
#    a. Launch overlay: ./compy.sh overlay
#    b. Press Cmd+Shift+Space — overlay opens top-right
#    c. Type "where is submitQuery" and press Enter
#    d. Should see ranked results with file:line
#    e. Click a result — jumps to editor at that line
#    f. Click the mic icon — red rings pulse, records 4s, transcribes
#    g. Press Esc — overlay dismisses
#    h. Press Cmd+Shift+Space again — fresh greeting, compact 72px window
#    i. Submit a nonsense query like "xyzzyblargh" — no-match hint appears
#    j. Check /tmp/compy-debug.log for daemon diagnostics
```

### Debug log

```sh
tail -f /tmp/compy-debug.log
# Shows daemon spawns, exit codes, output byte counts, errors
```

---

## How it works

```
Your question (voice or text)
        │
        ▼
  Parse → classify intent (reference, definition, fuzzy, history…)
        │
        ▼
  Grep / Graph / Git → find candidates (ripgrep, tree-sitter graph, git log)
        │
        ▼
  Reason → rank results (Freebuff → Ollama → heuristic → stub, cascading fallback)
        │
        ▼
  Ranked results → click to jump to file:line in editor
```

---

## Subcommands

| Command | What it does |
|---------|-------------|
| `./compy.sh overlay` | Build and launch the Swift/SwiftUI overlay |
| `./compy.sh build` | Run all 102 tests + compile extension + build overlay |
| `./compy.sh test` | Run daemon tests only |
| `./compy.sh listen` | Start UNIX socket listener (for extension selection) |
| `./compy.sh query` | Pipe a JSON query into the daemon |
| `./compy.sh ollama-start` | Start Ollama in background |
| `./compy.sh ollama-stop` | Stop the Ollama server |
| `./compy.sh ollama-status` | Check Ollama status and loaded models |
| `./compy.sh stt-test` | Record 3s from mic and transcribe via whisper.cpp |

---

## Features

- **Global hotkey** (Cmd+Shift+Space) — overlay opens top-right, gets keyboard focus
- **Voice + text input** — push-to-talk STT via whisper.cpp (offline, zero permissions)
- **7 intent types** — history, relational, references, definition, fuzzy, trace, rationale
- **6 backends** — Freebuff, Ollama, heuristic, stub, Graphify, Git history
- **Personality system** — greeting variation, staggered result reveals, haptic feedback,
  playful result headers, no-match hint pool, mic pulse rings
- **Face-state mascot** — 7 ASCII faces mapped to pipeline phases, smooth morph transitions,
  periodic blinking (every 3-5s, 15% wink chance), eye darting during processing
- **Compact/expand** — 72px input bar → 420px on results with smooth animation
- **Jump-to-editor** — click any result to open at file:line in agy-ide/cursor/code
- **Comment filtering** — grep results skip comment-only lines
- **CamelCase keyword splitting** — "handleRequest" splits to "handle" + "request" for better fuzzy search
- **Typing animation** — randomized quips during processing (16 messages across 3 pools)
- **Stale envelope rejection** — extension workspaceRoot validated by timestamp, falls back to git root

---

## Requirements

- macOS on Apple Silicon (M-series)
- Python 3.11+ with `tree-sitter`, `tree-sitter-python`, `networkx`
- Swift toolchain (Xcode or Command Line Tools)
- [Ollama](https://ollama.com) — optional, for LLM-ranked results
- [whisper-cpp](https://github.com/ggerganov/whisper.cpp) + ffmpeg — for voice input

No API keys. No subscriptions. No network required (Ollama + STT run locally).

---

## Docs

| File | Purpose |
|------|---------|
| [`.agent/SPEC.md`](.agent/SPEC.md) | Authoritative spec — architecture, data contracts, design |
| [`.agent/DEVLOG.md`](.agent/DEVLOG.md) | Design decisions across all sessions |
| [`.agent/UPDATES.md`](.agent/UPDATES.md) | Session-by-session changelog |
| [`compy/swift/README.md`](compy/swift/README.md) | Overlay build and architecture |
| [`compy/extension/README.md`](compy/extension/README.md) | Extension build and socket contract |
