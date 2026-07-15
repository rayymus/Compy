#!/usr/bin/env bash
# compy.sh — one-command build & launcher for Compy
#
# Usage:
#   ./compy.sh build        Build everything (daemon tests, extension, Swift overlay)
#   ./compy.sh test          Run daemon tests
#   ./compy.sh listen        Start the socket listener (for extension envelopes)
#   ./compy.sh overlay       Build & run the Swift overlay
#   ./compy.sh query         Run a single query via the daemon (JSON on stdin)
#   ./compy.sh install       Print install instructions for the extension + overlay
#   ./compy.sh ollama-start  Start Ollama server in background
#   ./compy.sh ollama-stop   Stop the Ollama server
#   ./compy.sh ollama-status Check if Ollama is running and what models are loaded
#   ./compy.sh stt-test      Test speech-to-text (records 3s from mic, transcribes)
#
# All subcommands exit 0 on success.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

build_daemon() {
    echo "=== compy daemon ==="
    cd "$ROOT"
    python3 -m pytest compy/daemon/tests -q
    echo "  daemon: tests pass"
}

build_extension() {
    echo "=== compy extension ==="
    cd "$ROOT/compy/extension"
    npm install --silent 2>/dev/null
    npm run build
    echo "  extension: built → compy/extension/out/extension.js"
}

build_overlay() {
    echo "=== compy overlay (Swift) ==="
    cd "$ROOT/compy/swift"
    swift build --quiet 2>&1
    echo "  overlay: built → compy/swift/.build/debug/Compy"
}

cmd_build() {
    build_daemon
    build_extension
    build_overlay
    echo ""
    echo "Compy build complete. Binary: compy/swift/.build/debug/Compy"
    echo "Run ./compy.sh listen in one terminal, then ./compy.sh overlay in another."
}

cmd_test() {
    cd "$ROOT"
    python3 -m pytest compy/daemon/tests -v
}

cmd_listen() {
    echo "Starting Compy socket listener on /tmp/compy-selection.sock ..."
    cd "$ROOT"
    python3 -m compy.daemon.socket_listener
}

cmd_overlay() {
    cd "$ROOT/compy/swift"
    swift build --quiet
    # Kill any stale overlay instances so only one runs.
    pkill -f "Compy$" 2>/dev/null && sleep 0.3 || true
    echo "Launching Compy overlay..."
    COMPY_SKIP_FREEBUFF=1 COMPY_ROOT="$ROOT" .build/debug/Compy &
    echo "  PID: $!"
    echo "  Press Cmd+Shift+Space to trigger."
    echo ""
    echo "  If the hotkey doesn't work from other apps, grant Accessibility permission:"
    echo "  System Settings → Privacy & Security → Accessibility → add Compy"
}

cmd_query() {
    cd "$ROOT"
    COMPY_SKIP_FREEBUFF=1 python3 -m compy.daemon --reasoner stub
}

cmd_install() {
    local ext_dir="$HOME/.vscode/extensions/compy-companion"
    echo "=== Compy Install Instructions ==="
    echo ""
    echo "1. Start Ollama (for LLM-ranked results):"
    echo "   ./compy.sh ollama-start"
    echo ""
    echo "2. Install the companion extension:"
    echo "   cp -r $ROOT/compy/extension $ext_dir"
    echo "   (or load via VS Code: 'Developer: Install Extension from Location...')"
    echo ""
    echo "3. Start the socket listener (keeps running in background):"
    echo "   $ROOT/compy.sh listen &"
    echo ""
    echo "4. Run the overlay:"
    echo "   $ROOT/compy.sh overlay"
    echo ""
    echo "5. Or, add Compy to your login items for auto-start:"
    echo "   System Settings → General → Login Items → +"
    echo "   → $ROOT/compy/swift/.build/debug/Compy"
    echo ""
    echo "Hotkey: Cmd+Shift+Space"
    echo "Backends: ollama → heuristic → stub (freebuff skipped via COMPY_SKIP_FREEBUFF=1 until -p flag ships)"
    echo "STT: whisper.cpp (tiny.en) via ffmpeg mic capture"
}

cmd_ollama_start() {
    if curl -s --max-time 2 http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "Ollama is already running on port 11434."
        return 0
    fi
    echo "Starting Ollama server..."
    nohup /opt/homebrew/bin/ollama serve > /tmp/ollama-serve.log 2>&1 &
    sleep 2
    if curl -s --max-time 3 http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "Ollama server started (PID: $!). Log: /tmp/ollama-serve.log"
    else
        echo "Warning: server may still be starting. Check /tmp/ollama-serve.log"
    fi
}

cmd_ollama_stop() {
    pkill -f "ollama serve" 2>/dev/null && echo "Ollama server stopped." || echo "No ollama process found."
}

cmd_ollama_status() {
    if curl -s --max-time 3 http://localhost:11434/api/tags 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    models = d.get('models', [])
    print(f'Ollama ONLINE — {len(models)} model(s) loaded')
    for m in models:
        size_gb = m.get('size', 0) / 1e9
        print(f'  {m[\"name\"]} ({size_gb:.1f} GB)')
except: print('Ollama OFFLINE or unreachable')
" 2>/dev/null; then :; else
        echo "Ollama OFFLINE. Start with: ./compy.sh ollama-start"
    fi
}

cmd_stt_test() {
    echo "Recording 3 seconds from default microphone..."
    echo "Speak a short query now."
    cd "$ROOT"
    python3 -m compy.daemon.stt --duration 3
}

case "${1:-build}" in
    build)         cmd_build ;;
    test)          cmd_test ;;
    listen)        cmd_listen ;;
    overlay)       cmd_overlay ;;
    query)         cmd_query ;;
    install)       cmd_install ;;
    ollama-start)  cmd_ollama_start ;;
    ollama-stop)   cmd_ollama_stop ;;
    ollama-status) cmd_ollama_status ;;
    stt-test)      cmd_stt_test ;;
    *)
        echo "usage: compy.sh {build|test|listen|overlay|query|install|ollama-start|ollama-stop|ollama-status|stt-test}"
        exit 1
        ;;
esac
