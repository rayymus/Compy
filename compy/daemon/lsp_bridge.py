"""LSP bridge — query the editor's language server through the Compy extension.

Per claude-response5.md §2-3: the daemon requests LSP data from the extension
via a Unix socket.  The extension executes VS Code LSP commands (definition,
references, hover) and returns results.  This module handles the daemon side:
connect, send request, read response, with a short timeout so LSP never blocks
the sub-second search budget (§3: startup latency, no-editor, per-project).

Results are returned as GrepHit tuples so they plug into the existing enrichment
pipeline without any structural changes.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from .models import GrepHit

DEFAULT_LSP_SOCKET = "/tmp/compy-lsp.sock"
LSP_TIMEOUT = 2.0  # seconds — never block a query longer than this (§3)


class LspUnavailable(Exception):
    """Raised when the LSP bridge cannot serve a request right now.

    Graceful fallback: the orchestrator catches this and falls through to
    tree-sitter/grep enrichment (the existing path).
    """


def _lsp_socket_path() -> str:
    return os.environ.get("COMPY_LSP_SOCKET", DEFAULT_LSP_SOCKET)


def query_lsp(
    query_type: str,
    symbol: str = "",
    *,
    file: str | None = None,
    line: int | None = None,
    new_name: str | None = None,
    timeout: float = LSP_TIMEOUT,
) -> tuple[GrepHit, ...]:
    """Send an LSP query to the editor extension and return results.

    query_type: "definition", "references", "hover", or "rename".
    Returns empty tuple on timeout, connection failure, or no results.
    Raises LspUnavailable if the socket doesn't exist (no editor connected).
    """
    sock_path = _lsp_socket_path()
    if not os.path.exists(sock_path):
        raise LspUnavailable(f"LSP bridge socket not found: {sock_path}")

    request: dict[str, Any] = {"type": query_type, "symbol": symbol}
    if file:
        request["file"] = file
    if line is not None:
        request["line"] = line
    if new_name:
        request["newName"] = new_name

    payload = json.dumps(request, ensure_ascii=False).encode("utf-8")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sock_path)
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)  # signal end of send

        data = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                break
    except (socket.timeout, ConnectionRefusedError, OSError):
        return ()
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if not data:
        return ()

    try:
        response = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ()

    if not response.get("ok"):
        return ()

    results = response.get("results", [])
    if not results:
        return ()

    hits: list[GrepHit] = []
    for r in results:
        hits.append(GrepHit(
            file=r.get("file", ""),
            line=r.get("line", 0),
            column=0,
            snippet=r.get("snippet", "")[:300],
        ))
    return tuple(hits)


def enrich_with_lsp(
    symbol: str,
    *,
    file: str | None = None,
    line: int | None = None,
    timeout: float = LSP_TIMEOUT,
) -> list[str]:
    """Enrich a symbol with LSP data: definition, references, hover.

    Returns a list of context strings like "Defined in: auth.py:42",
    "Referenced by 3 files", "Hover: async def handle_request(user_id)".

    Non-blocking: falls back to empty list on any failure.
    Graceful degradation is the contract — never let LSP latency threaten
    the search budget (§3).
    """
    ctx_parts: list[str] = []

    # Definition: where is this defined?
    try:
        defs = query_lsp("definition", symbol, file=file, line=line, timeout=timeout)
        if defs:
            d = defs[0]
            ctx_parts.append(f"Defined in: {d.file}:{d.line}")
    except LspUnavailable:
        pass

    # References: what else references this?
    try:
        refs = query_lsp("references", symbol, file=file, line=line, timeout=timeout)
        if refs:
            files = {r.file.rsplit("/", 1)[-1] for r in refs[:5]}
            ctx_parts.append(f"Referenced by: {', '.join(sorted(files))}")
    except LspUnavailable:
        pass

    # Hover: what does the language server say about this?
    try:
        hovers = query_lsp("hover", symbol, file=file, line=line, timeout=timeout)
        if hovers:
            h = hovers[0]
            if h.snippet and len(h.snippet) > 10:
                ctx_parts.append(f"LSP: {h.snippet[:120]}")
    except LspUnavailable:
        pass

    return ctx_parts
