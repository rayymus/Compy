# Compy Swift Overlay

`OverlayPanel.swift`, `HotkeyManager.swift`, and `Models.swift` are the native
macOS-only Compy overlay per `.agent/SPEC.md` §5a (Swift/SwiftUI).

## What's shipped

- **OverlayPanel.swift** — CompyPanel (NSPanel, floating, nonactivating, top-right),
  OverlayState (mic/text modes, empty/processing/results/noMatch/degraded phases),
  OverlayView (input bar, result rows with hover, click-to-open in editor).
- **HotkeyManager.swift** — NSEvent global monitor on Cmd+Shift+Space + clipboard-swap
  fallback + selection JSON file reading from companion extension.
- **Models.swift** — Codable mirror of daemon data contracts (RankedHit, QueryRequest,
  Selection with snake_case CodingKeys, QueryResult, SelectionEnvelope).

## Overlay features

- Esc key dismiss (NSEvent local monitor)
- Click-outside dismiss (NSWindowDelegate windowDidResignKey)
- Cmd+Shift+Space toggle (global hotkey)
- Result click-to-open in editor (agy-ide/cursor/code -g)
- Reasoner source badge (color-coded by backend)
- STT push-to-talk mic mode (whisper.cpp + ffmpeg via `compy.daemon.stt`)
- Degraded banner, no-match state, result count header
- Spring/easeOut animations on state transitions

## Build

```sh
cd compy/swift
swift build        # → .build/debug/Compy (632KB Mach-O arm64)
```

Or via the launcher: `./compy.sh overlay`

## Architecture

The overlay communicates with the daemon via subprocess:
```
Swift overlay → Process("python3 -m compy.daemon")
             → stdin: QueryRequest JSON
             → stdout: QueryResult JSON
             → renders ranked hits
```

STT uses the same pattern:
```
Swift overlay → Process("python3 -m compy.daemon.stt")
             → stdout: {"text": "...", "success": true}
```

## Not yet built (v1.1)

- Live STT streaming (currently push-to-talk 3s recording)
- MLX-backed inline parser (uses Python daemon's RuleBasedParser)
