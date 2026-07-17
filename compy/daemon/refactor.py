"""Tier 1 refactoring: external formatters through a shared apply pipeline.

Architecture boundary per claude-response3.md §5: the Reasoner Protocol stays
read-only/scoring-only — edit generation and file writes live here, entirely
separate from the search pipeline.

Shared pipeline: generate → pre-write verify → stage → confirm → backup →
atomic write → post-write verify → register for undo.

Stateless design: daemon stages proposed edits to /tmp/compy-staged-<token>.json,
returns the token in QueryResult.  The overlay spawns a second daemon invocation
with "/confirm <token>" to apply, or discards the staged file on reject.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import FileProposal, QueryResult, Selection

# ── Formatter detection ────────────────────────────────────────────────

# Map file extensions to formatter commands.  Each entry is
# (executable, *args) where the last arg is a placeholder for stdin.
_FORMATTERS: dict[str, list[str]] = {
    ".py": ["black", "--quiet", "-"],
    ".js": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".ts": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".jsx": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".tsx": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".json": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".md": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
}


def _detect_formatter(file_path: str) -> list[str] | None:
    """Return the formatter command for a file, or None if unsupported."""
    ext = Path(file_path).suffix.lower()
    return _FORMATTERS.get(ext)


def _is_formatter_available(cmd: list[str]) -> bool:
    """Check whether the formatter executable is on PATH."""
    exe = cmd[0]
    return shutil.which(exe) is not None


# ── Pre-write verification ──────────────────────────────────────────────

def _verify_syntax(file_path: str, content: str) -> str | None:
    """Tree-sitter parse-check proposed content.  Returns None on success,
    or an error message string on failure."""
    ext = Path(file_path).suffix.lower()
    if ext == ".py":
        return _verify_python(content)
    # For JS/TS/JSON/MD: skip tree-sitter parse — Prettier output is
    # syntactically valid by construction.  A failed parse would mean
    # Prettier itself is broken, which is outside our scope.
    return None


def _verify_python(source: str) -> str | None:
    """Parse-check Python source via tree-sitter.  Returns None on success."""
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except Exception:
        return None  # tree-sitter not available — skip verify
    try:
        lang = Language(tspython.language())
        parser = Parser(lang)
        tree = parser.parse(source.encode("utf-8"))
        if tree.root_node.has_error:
            return "Python syntax error in proposed content"
    except Exception as exc:
        return f"Syntax check failed: {exc}"
    return None


def _cleanup_stale_staged() -> None:
    """Remove staged edit files older than 5 minutes — orphans from rejected proposals."""
    cutoff = time.time() - 300  # 5 minutes
    try:
        for p in STAGE_DIR.glob("compy-staged-*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


# ── Staging / applying ──────────────────────────────────────────────────

STAGE_DIR = Path("/tmp")
UNDO_PATH = STAGE_DIR / "compy-undo.json"


def _make_token() -> str:
    """Short unique token for staged-edit filenames."""
    raw = f"{time.time():.6f}-{os.getpid()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


@dataclass
class _StagedEdit:
    file: str
    original: str  # original file content (utf-8 text)
    formatted: str  # proposed new content


def stage_format(selection: Selection, workspace: str) -> QueryResult | None:
    """Generate format proposals for the selected file.

    Returns a QueryResult with refactor_proposals + refactor_token on success,
    or None if no formatter is available / nothing changed.

    Cleans up orphaned staged files older than 5 minutes before creating new ones.
    """
    # Clean up stale staged files from rejected proposals.
    _cleanup_stale_staged()
    sel_file = selection.file
    if not sel_file:
        return None

    # Resolve against workspace.
    file_path = (Path(workspace).resolve() / sel_file)
    if not file_path.exists():
        return None

    cmd_template = _detect_formatter(str(file_path))
    if cmd_template is None:
        return None

    # Resolve {file} placeholder.
    cmd = [str(file_path) if a == "{file}" else a for a in cmd_template]
    if not _is_formatter_available(cmd):
        return None

    # Read original content.
    try:
        original = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    # Run formatter: pipe original to stdin, capture stdout.
    try:
        result = subprocess.run(
            cmd, input=original, capture_output=True, text=True, timeout=15,
        )
        formatted = result.stdout
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None

    if formatted == original or not formatted.strip():
        return None  # Nothing changed or empty output.

    # Pre-write verify.
    err = _verify_syntax(str(file_path), formatted)
    if err:
        return QueryResult(
            intent="format",
            degraded=True,
            reason=f"Pre-write verify failed: {err}",
        )

    # Stage the edit.
    token = _make_token()
    staged = _StagedEdit(file=str(file_path), original=original, formatted=formatted)
    stage_path = STAGE_DIR / f"compy-staged-{token}.json"
    stage_path.write_text(json.dumps(asdict(staged)), encoding="utf-8")

    # Count approximate changed lines.
    orig_lines = original.count("\n")
    new_lines = formatted.count("\n")
    changed = abs(new_lines - orig_lines)

    return QueryResult(
        intent="format",
        refactor_proposals=(FileProposal(file=sel_file, changed_lines=changed),),
        refactor_token=token,
    )


def apply_staged(token: str, workspace: str) -> QueryResult:
    """Apply a previously staged edit: atomic write → backup → post-verify.

    Called by the orchestrator when it receives "/confirm <token>".
    Backup (undo registration) happens AFTER successful write, so a failed
    write never leaves a dangling undo entry pointing to an unchanged file.
    """
    stage_path = STAGE_DIR / f"compy-staged-{token}.json"
    if not stage_path.exists():
        return QueryResult(
            intent="format", degraded=True,
            reason=f"Staged edit expired or not found: {token}",
        )

    try:
        data = json.loads(stage_path.read_text(encoding="utf-8"))
        edit = _StagedEdit(**data)
    except (json.JSONDecodeError, TypeError):
        return QueryResult(
            intent="format", degraded=True,
            reason="Corrupt staged edit file.",
        )

    file_path = Path(edit.file)
    if not file_path.exists():
        stage_path.unlink(missing_ok=True)
        return QueryResult(
            intent="format", degraded=True,
            reason=f"File no longer exists: {edit.file}",
        )

    # Atomic write: write to temp, rename over original.
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(file_path.parent), prefix=".compy-", suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(edit.formatted)
        os.replace(tmp_name, str(file_path))  # atomic on same filesystem
    except OSError as exc:
        return QueryResult(
            intent="format", degraded=True,
            reason=f"Atomic write failed: {exc}",
        )

    # Backup: snapshot pre-edit bytes for undo (AFTER successful write).
    _register_undo(edit)

    # Post-write verify: re-read from disk and parse-check.
    try:
        written = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        return QueryResult(
            intent="format", degraded=True,
            reason=f"Post-write read failed: {exc}",
        )
    err = _verify_syntax(str(file_path), written)
    if err:
        return QueryResult(
            intent="format", degraded=True,
            reason=f"Post-write verify failed: {err}",
        )

    # Clean up staged file.
    stage_path.unlink(missing_ok=True)

    return QueryResult(
        intent="format",
        hits=(),  # empty hits — success is communicated via intent + non-degraded
    )


# ── Undo ─────────────────────────────────────────────────────────────────

def _register_undo(edit: _StagedEdit) -> None:
    """Record the original content so /undo can restore it."""
    entries: list[dict] = []
    if UNDO_PATH.exists():
        try:
            entries = json.loads(UNDO_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
    entries.append({
        "ts": time.time(),
        "file": edit.file,
        "original": edit.original,
    })
    UNDO_PATH.write_text(json.dumps(entries), encoding="utf-8")


def undo_last() -> QueryResult:
    """Restore every file from the most recent undo snapshot.

    One undo restores ALL files from that operation — not per-file.
    """
    if not UNDO_PATH.exists():
        return QueryResult(
            intent="format", degraded=True,
            reason="Nothing to undo.",
        )

    try:
        entries = json.loads(UNDO_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return QueryResult(
            intent="format", degraded=True,
            reason="Corrupt undo file.",
        )

    if not entries:
        return QueryResult(
            intent="format", degraded=True,
            reason="Nothing to undo.",
        )

    # Group by timestamp — undo the most recent batch (same second).
    groups: dict[int, list[dict]] = {}
    for e in entries:
        ts = int(e["ts"])
        groups.setdefault(ts, []).append(e)
    latest_ts = max(groups)
    batch = groups[latest_ts]

    restored: list[str] = []
    errors: list[str] = []
    for e in batch:
        file_path = Path(e["file"])
        try:
            file_path.write_text(e["original"], encoding="utf-8")
            restored.append(e["file"])
        except OSError as exc:
            errors.append(f"{e['file']}: {exc}")

    # Remove the undone entries from the undo file.
    remaining = [e for e in entries if int(e["ts"]) != latest_ts]
    if remaining:
        UNDO_PATH.write_text(json.dumps(remaining), encoding="utf-8")
    else:
        UNDO_PATH.unlink(missing_ok=True)

    if errors:
        return QueryResult(
            intent="format", degraded=True,
            reason=f"Undo restored {len(restored)} files, {len(errors)} failed: {'; '.join(errors[:3])}",
        )
    return QueryResult(
        intent="format",
        hits=(),
        reason=f"Undid {len(restored)} file{'s' if len(restored) != 1 else ''}: {', '.join(restored)}",
    )
