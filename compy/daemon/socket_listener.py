"""UNIX socket listener for Compy selection envelopes.

The VS Code companion extension writes `{file, line, workspaceRoot, selectedText, ts}`
to `/tmp/compy-selection.sock` on Cmd+Shift+Space. This listener caches the latest
envelope to `/tmp/compy-selection.json` so the Swift overlay and daemon can read it.

Run as:  python3 -m compy.daemon.socket_listener [--socket /tmp/compy-selection.sock]
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
from pathlib import Path

DEFAULT_SOCKET = "/tmp/compy-selection.sock"
DEFAULT_CACHE = "/tmp/compy-selection.json"


def run(socket_path: str = DEFAULT_SOCKET, cache_path: str = DEFAULT_CACHE) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    # Clean up stale socket file.
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass

    sock.bind(socket_path)
    sock.listen(1)
    os.chmod(socket_path, 0o666)

    print(f"Compy socket listener: {socket_path}", file=sys.stderr)

    def _handle(_sig: int, _frame: object) -> None:
        print("", file=sys.stderr)
        sock.close()
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    while True:
        try:
            conn, _addr = sock.accept()
        except OSError:
            break
        with conn:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if len(data) > 64 * 1024:
                    break  # 64KB cap — envelopes are ~1KB
            if data:
                try:
                    envelope = json.loads(data.decode("utf-8"))
                    Path(cache_path).write_text(
                        json.dumps(envelope, indent=2), encoding="utf-8"
                    )
                    print(
                        f"  ← selection from {envelope.get('file', '?')}:{envelope.get('line', '?')}",
                        file=sys.stderr,
                    )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass


if __name__ == "__main__":
    run()
