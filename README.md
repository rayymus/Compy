# 🧠 Compy

> A global-hotkey code-search overlay for macOS. Press **⌘⇧Space**, ask about your codebase in voice or text, and jump directly to the answer. Zero cost. Fully offline-capable.

<p align="center">
  <img src="https://img.shields.io/badge/tests-173%20pass%2C%201%20skip-brightgreen" alt="Tests">
  <img src="https://img.shields.io/badge/license-MIT-blue" alt="License">
  <img src="https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/swift-6.0-orange" alt="Swift">
  <img src="https://img.shields.io/badge/python-3.12+-blue" alt="Python">
</p>

---

## Table of Contents

- [What is Compy?](#what-is-compy)
- [Architecture](#architecture)
- [Getting Started](#getting-started)
- [Usage](#usage)
- [Features](#features)
- [Commands](#commands)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License & Acknowledgments](#license--acknowledgments)

---

## What is Compy?

Compy is a Spotlight-style overlay that answers codebase questions without leaving your editor. Think of it as a code-aware search engine that understands your project's structure — call graphs, git history, symbol relationships — not just text matching.

**Who it's for:** Developers working in medium-to-large codebases who need to answer questions like "where else is this used?" or "what calls this function?" without grepping manually or context-switching to a browser.

**What problem it solves:** Code search tools either give you raw grep (no structure) or require setup/API keys/subscriptions (not zero-cost). Compy gives you structural understanding for free, running entirely on your machine.

### Key Features

- **Voice & text input** — speak or type queries naturally. Push-to-talk STT via whisper.cpp runs fully offline.
- **Structural code search** — understands call graphs, definitions, references, imports, inheritance via tree-sitter + NetworkX.
- **Git-aware** — ask "who added this?" or "why was this changed?" for blame and commit history.
- **Zero cost, zero network** — Ollama and whisper.cpp run locally. No API keys. No subscriptions.
- **Click-to-jump** — every result opens at `file:line` in your editor (VS Code / Antigravity IDE / Cursor).

### Tech Stack

| Layer | Technology |
|-------|-----------|
| **Overlay** | Swift + SwiftUI (native macOS panel) |
| **Daemon** | Python 3.12+ (parse → grep → reason pipeline) |
| **Extension** | TypeScript (VS Code API) |
| **Search** | ripgrep |
| **Code graph** | tree-sitter (5 languages) + NetworkX |
| **Ranking** | Ollama (qwen2.5-coder:1.5b) + heuristic fallback |
| **Voice** | whisper.cpp + ffmpeg |

---

## Architecture

```
┌─────────────────────────────────────────────┐
│          Overlay (Swift/SwiftUI)              │
│  NSPanel, top-right, Cmd+Shift+Space          │
└──────────────────┬──────────────────────────┘
                   │ JSON stdin/stdout
┌──────────────────▼──────────────────────────┐
│          Daemon (Python)                      │
│                                               │
│  parse ──→ grep ──→ reason ──→ ranked hits   │
│    │         │         │                      │
│    │    ripgrep    ollama→heuristic→stub       │
│    │    + graph    + Freebuff (optional)       │
│                                               │
│  Backends:                                     │
│  • Graphify — tree-sitter → call graph        │
│  • GitHistory — log, blame                    │
│  • LSP bridge — live editor symbols           │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│      Editor Extension (TypeScript)            │
│  Selection capture, LSP socket, refactoring    │
└─────────────────────────────────────────────┘
```

### Intent Routing

The parser classifies each query into one of 16 intents, routed to the appropriate backend:

| Intent | Example Query | Backend |
|--------|--------------|---------|
| `references` | "where else is `get_ability` used?" | ripgrep |
| `definition` | "where is `handle_request` defined?" | smart grep patterns |
| `relational` | "what calls `validate_token`?" | Graphify |
| `explain` | "explain this function" | Graphify (def + callers + callees) |
| `graph_path` | "how are `login` and `verify` connected?" | Graphify (shortest path) |
| `overview` | "how does this codebase work?" | Graphify (module map) |
| `blast_radius` | "what breaks if I change this?" | Graphify |
| `dead_code` | "what's unused?" | Graphify |
| `history` | "who added this null check?" | GitHistory |
| `rationale` | "why does this exist?" | GitHistory → fuzzy |
| `trace` | paste stack trace → jump to frames | zero-LLM file:line extraction |
| `fuzzy` | "where's the function that validates tokens?" | ripgrep → reasoner ranking |
| `rename` | "rename `foo` to `bar`" | Graphify + tree-sitter |
| `format` | "format this file" | Black / Prettier |
| `extract_variable` | "extract this expression" | string manipulation |
| `add_type_hints` | "add type hints" | tree-sitter |

---

## Getting Started

### Prerequisites

- **macOS** on Apple Silicon (M-series)
- **Python 3.12+** with pip
- **Swift toolchain** (Xcode or Command Line Tools)
- **ripgrep** — `brew install ripgrep`

Optional (recommended):

- **Ollama** — `brew install ollama` (for LLM-ranked results)
- **whisper-cpp + ffmpeg** — `brew install whisper-cpp ffmpeg` (for voice input)

### Installation

```bash
# Clone the repo
git clone https://github.com/raymus/compy.git
cd compy
chmod +x compy.sh

# Install Python dependencies
pip install -r requirements.txt

# Install system tools
brew install ripgrep                    # required
brew install ollama whisper-cpp ffmpeg  # optional

# Pull the Ollama ranking model
ollama pull qwen2.5-coder:1.5b

# Build and launch
./compy.sh overlay
```

Press **⌘⇧Space** to open the overlay. Type a question, press Enter, click any result to jump to source.

### Editor Extension

For selection capture (current file, line, workspace), install the VS Code extension:

```bash
cd compy/extension && npm install && npm run build
```

Then **Cmd+Shift+P → "Developer: Install Extension from Location..."** → select `compy/extension/`.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPY_ROOT` | `compy/` directory | Fallback workspace root |
| `COMPY_OLLAMA_MODEL` | `qwen2.5-coder:1.5b` | Ollama model for ranking |
| `COMPY_SKIP_FREEBUFF` | `1` | Skip Freebuff (no `-p` flag in CLI) |

---

## Usage

### Basic Search

```
Press ⌘⇧Space → type "where is submitQuery defined?" → Enter → click result
```

### Voice Input

```
Press ⌘⇧Space → click mic icon (or stay in mic mode) → speak → auto-transcribes → auto-submits
```

### Follow-up Questions

The overlay stays open after results. Type a follow-up immediately — previous results remain dimmed for context.

### Workspace Switching

```
# Natural language
"find auth in garden_warriors/"

# Explicit command
/workspace ~/projects/my-app
```

### Stack Trace Jump

```
Paste any Python/Node.js/Go/Rust stack trace → Compy extracts file:line frames → click to jump directly
```

### Testing

```bash
# Full test suite (173 tests)
./compy.sh test

# Build everything (tests + extension + overlay)
./compy.sh build

# Stop all processes
./compy.sh stop
```

---

## Features

### Search & Discovery
- 16 intent types — from simple references to structural code explainer
- Smart grep patterns — "definition" intent auto-generates `def\s+X\b`, `class\s+X\b`
- CamelCase & kebab-case aware keyword extraction
- Synonym expansion — "bug" also matches "error", "exception"
- Suffix stemming — "authenticated" matches "authenticate"
- Comment filtering — grep skips comment-only lines
- N-gram adjacency ranking — respects word order

### Structural Understanding
- **Code explainer** — select a function, ask "what does this do?" → definition + callers + callees
- **Graph path** — "how are X and Y connected?" → shortest path through call graph
- **Blast radius** — "what depends on X?" → all callers, importers, subclasses
- **Catch-up Q&A** — "how does this codebase work?" → module map with key symbols
- **Dead code detection** — "what's unused?" → zero-reference functions
- 5-language tree-sitter: Python, JavaScript, TypeScript, Rust, Go

### Refactoring
- **Tier 1**: Format files (Black for Python, Prettier for JS/TS/JSON/MD)
- **Tier 2**: Graph-verified multi-file rename with undo
- **Tier 3**: Inline suggestions — extract variable, add type hints

### Working Set Engine
- Session-scoped activation scores with Personalized PageRank
- Topic-shift detection prevents tunnel vision
- Click feedback loop — interacting with results biases future ranking
- Next-question suggestions from active nodes

### Personality & UX
- ASCII face-state mascot with 10+ expressions, blinking, eye darting
- Smooth morph transitions between pipeline phases
- Tier-of-origin face — different expressions for LLM vs heuristic vs graph results
- Staggered result reveals, haptic feedback, typing animation quips
- Compact 72px → expanded 420px with smooth animation
- Cinematic intro animation (face pop-in → glide to corner)

---

## Commands

| Command | Description |
|---------|-------------|
| `./compy.sh overlay` | Build and launch the Swift overlay |
| `./compy.sh build` | Run all tests + compile extension + build overlay |
| `./compy.sh test` | Run daemon tests only |
| `./compy.sh listen` | Start UNIX socket listener for extension |
| `./compy.sh query` | Pipe a JSON query into the daemon |
| `./compy.sh ollama-start` | Start Ollama in background |
| `./compy.sh ollama-stop` | Stop Ollama server |
| `./compy.sh ollama-status` | Check Ollama status and loaded models |
| `./compy.sh stt-test` | Record 3s from mic and transcribe |
| `./compy.sh stop` | Kill all Compy processes |

---

## Roadmap

- [ ] **P5: ML parser** — replace regex-based parser with local model (MLX/Ollama)
- [ ] **Live STT streaming** — real-time transcription instead of push-to-talk bursts
- [ ] **Embedding-based search** — semantic similarity beyond token overlap
- [ ] **Cross-platform** — Linux and Windows support
- [ ] **NDJSON framing** — robust stdout protocol instead of fragile `lines.last`

---

## Contributing

Contributions are welcome. The project follows a structured workflow documented in `.agent/WORKFLOW.md`.

### Quick Start for Contributors

1. Read `.agent/onboarding.html` — project overview and architecture (note: test count shown there is outdated; current: 173)
2. Read `.agent/AGENTS.md` — critical mistakes to avoid and personality spec
3. Read `.agent/SPEC.md` — authoritative architecture and data contracts
4. Make changes, run `./compy.sh build`, open a PR

### Project Structure

```
compy/
├── swift/Sources/Compy/     # SwiftUI overlay
│   ├── OverlayPanel.swift   # Main panel, face system, daemon bridge
│   ├── HotkeyManager.swift  # Global hotkey, AX workspace detection
│   └── Models.swift         # Codable types
├── daemon/                  # Python pipeline
│   ├── orchestrator.py      # Main pipeline: parse → grep → reason
│   ├── parser.py            # RuleBasedParser, 16 intents
│   ├── grepper.py           # ripgrep wrapper
│   ├── reasoner.py          # Ollama/Freebuff/Stub reasoners
│   ├── heuristic_reasoner.py # Jaccard offline ranker
│   ├── graphify.py          # tree-sitter → NetworkX graph
│   ├── gitlog.py            # git log/blame
│   ├── refactor.py          # Format, rename, extract variable
│   ├── stt.py               # whisper.cpp speech-to-text
│   └── tests/               # 173 tests
├── extension/src/           # VS Code extension
│   └── extension.ts         # Selection capture, LSP bridge
└── .agent/                  # Agent workflow files (docs + handover)
```

---

## License & Acknowledgments

MIT License — see [LICENSE](LICENSE) for details.

Built with:
- [ripgrep](https://github.com/BurntSushi/ripgrep) — fast code search
- [tree-sitter](https://tree-sitter.github.io/) — incremental parsing
- [NetworkX](https://networkx.org/) — graph algorithms
- [Ollama](https://ollama.com/) — local LLM inference
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) — offline speech recognition
- [SwiftUI](https://developer.apple.com/xcode/swiftui/) — native macOS overlay

---

<p align="center">
  <sub>Built with ❤️ for developers who live in their editor</sub>
</p>
