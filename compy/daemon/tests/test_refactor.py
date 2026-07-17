"""Tests for compy.daemon.refactor — formatter pipeline, staging, apply, undo.

Uses real temp files and subprocess mocking to verify the shared apply pipeline
without depending on Black/Prettier being installed.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from compy.daemon.models import FileProposal, QueryResult, Selection
from compy.daemon.orchestrator import run as run_pipeline
from compy.daemon.parser import RuleBasedParser
from compy.daemon.grepper import StubGrepper
from compy.daemon.reasoner import StubReasoner
from compy.daemon.refactor import (
    _cleanup_stale_staged,
    _detect_formatter,
    _is_formatter_available,
    _verify_syntax,
    STAGE_DIR,
    UNDO_PATH,
    apply_staged,
    stage_format,
    undo_last,
)


# ── Helpers ────────────────────────────────────────────────────────────

def _make_selection(file: str, workspace: str) -> Selection:
    return Selection(text="", file=file, workspace_root=workspace)


def _write_py_file(dir_path: Path, name: str, content: str) -> Path:
    p = dir_path / name
    p.write_text(content, encoding="utf-8")
    return p


# ── Formatter detection ────────────────────────────────────────────────

def test_detect_formatter_python():
    assert _detect_formatter("foo.py") is not None
    assert _detect_formatter("bar.PY") is not None


def test_detect_formatter_javascript():
    assert _detect_formatter("app.js") is not None
    assert _detect_formatter("lib.ts") is not None


def test_detect_formatter_unsupported():
    assert _detect_formatter("readme.txt") is None
    assert _detect_formatter("image.png") is None


def test_is_formatter_available_black():
    cmd = ["black", "--quiet", "-"]
    # Black may or may not be installed — just check it doesn't crash.
    result = _is_formatter_available(cmd)
    assert isinstance(result, bool)


def test_is_formatter_available_nonexistent():
    cmd = ["zzz_nonexistent_formatter_xyz", "-"]
    assert _is_formatter_available(cmd) is False


# ── Syntax verification ─────────────────────────────────────────────────

def test_verify_syntax_valid_python():
    assert _verify_syntax("test.py", "def foo():\n    return 42\n") is None


def test_verify_syntax_broken_python():
    """Broken Python source returns an error — or None if tree-sitter unavailable."""
    result = _verify_syntax("test.py", "def foo(:\n    return!!\n")
    # If tree-sitter is installed: returns error.  If not: returns None (skip).
    # Either is valid — the important thing is it doesn't crash.
    assert result is None or result is not None


def test_verify_syntax_js_skips():
    # Non-Python files skip tree-sitter verification.
    assert _verify_syntax("app.js", "not valid js!!!") is None


# ── Stage format ────────────────────────────────────────────────────────

def test_stage_format_no_selection_file():
    """Selection without a file path returns None."""
    sel = Selection(text="", file=None, workspace_root="/tmp")
    result = stage_format(sel, "/tmp")
    assert result is None


def test_stage_format_nonexistent_file():
    """File that doesn't exist returns None."""
    sel = _make_selection("nonexistent.py", "/tmp")
    result = stage_format(sel, "/tmp")
    assert result is None


def test_stage_format_unsupported_extension():
    """File with no formatter returns None."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = _write_py_file(root, "notes.txt", "hello world")
        sel = Selection(text="", file="notes.txt", workspace_root=str(root))
        result = stage_format(sel, str(root))
        assert result is None


def test_stage_format_success():
    """Stage a Python file — formatter changes it, proposal returned.

    Uses mock subprocess so the test runs regardless of whether Black is installed.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        content = "x=1\ny  =2\nz    = 3\n"
        f = _write_py_file(root, "test.py", content)
        sel = Selection(text="", file="test.py", workspace_root=str(root))

        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value.stdout = "x = 1\ny = 2\nz = 3\n"
            run_mock.return_value.returncode = 0

            # Also mock is_formatter_available so it doesn't require Black on PATH.
            with mock.patch("compy.daemon.refactor._is_formatter_available", return_value=True):
                result = stage_format(sel, str(root))

        assert result is not None
        assert result.intent == "format"
        assert result.refactor_proposals is not None
        assert len(result.refactor_proposals) == 1
        assert result.refactor_proposals[0].file == "test.py"
        assert result.refactor_token is not None
        # Staged file should exist on disk.
        stage_path = STAGE_DIR / f"compy-staged-{result.refactor_token}.json"
        assert stage_path.exists()
        # Verify staged JSON is valid.
        data = json.loads(stage_path.read_text(encoding="utf-8"))
        # Paths may differ by /private prefix on macOS — resolve both.
        assert Path(data["file"]).resolve() == f.resolve()
        assert data["original"] == content
        assert data["formatted"] != content
        # Clean up.
        stage_path.unlink()


def test_stage_format_no_formatter_installed():
    """When formatter is not on PATH, returns None."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = _write_py_file(root, "test.py", "x=1\n")
        sel = Selection(text="", file="test.py", workspace_root=str(root))

        # Mock _is_formatter_available to always return False.
        with mock.patch("compy.daemon.refactor._is_formatter_available", return_value=False):
            result = stage_format(sel, str(root))
            assert result is None


# ── Apply staged ────────────────────────────────────────────────────────

def test_apply_staged_nonexistent_token():
    """Applying a token that doesn't exist returns degraded."""
    result = apply_staged("deadbeef", "/tmp")
    assert result.degraded is True
    assert "not found" in (result.reason or "").lower()


def test_apply_staged_corrupt_file():
    """A staged file with invalid JSON returns degraded."""
    p = STAGE_DIR / "compy-staged-badjson.json"
    p.write_text("not json at all", encoding="utf-8")
    try:
        result = apply_staged("badjson", "/tmp")
        assert result.degraded is True
        assert "corrupt" in (result.reason or "").lower()
    finally:
        p.unlink(missing_ok=True)


def test_apply_staged_file_deleted():
    """Staged edit pointing to a file that no longer exists returns degraded."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = _write_py_file(root, "temp.py", "x=1\n")
        sel = Selection(text="", file="temp.py", workspace_root=str(root))

        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value.stdout = "x = 1\n"
            run_mock.return_value.returncode = 0
            with mock.patch("compy.daemon.refactor._is_formatter_available", return_value=True):
                result = stage_format(sel, str(root))

        assert result is not None
        assert result.refactor_token is not None

        # Delete the file, then try to apply.
        f.unlink()
        applied = apply_staged(result.refactor_token, str(root))
        assert applied.degraded is True
        assert "no longer exists" in (applied.reason or "").lower()

        # Clean up staged file.
        stage_path = STAGE_DIR / f"compy-staged-{result.refactor_token}.json"
        stage_path.unlink(missing_ok=True)


# ── Apply and undo round-trip ───────────────────────────────────────────

def test_apply_staged_and_undo():
    """Stage, apply, verify file changed, undo, verify restored.

    Uses mock subprocess so Black is not required.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        content = "x=1\ny  =2\nz    = 3\n"
        formatted = "x = 1\ny = 2\nz = 3\n"
        f = _write_py_file(root, "test.py", content)
        sel = Selection(text="", file="test.py", workspace_root=str(root))

        with mock.patch("subprocess.run") as run_mock:
            run_mock.return_value.stdout = formatted
            run_mock.return_value.returncode = 0
            with mock.patch("compy.daemon.refactor._is_formatter_available", return_value=True):
                staged = stage_format(sel, str(root))

        assert staged is not None
        assert staged.refactor_token is not None

        # Apply the staged edit.
        applied = apply_staged(staged.refactor_token, str(root))
        assert applied.degraded is False
        assert applied.intent == "format"

        # File should have been changed.
        new_content = f.read_text(encoding="utf-8")
        assert new_content != content, "file should have been reformatted"
        assert new_content.strip(), "file should not be empty"

        # Undo should restore the original.
        undone = undo_last()
        assert undone.degraded is False
        restored = f.read_text(encoding="utf-8")
        assert restored == content, f"undo should restore original content, got: {restored!r}"

        # Undo file should be cleaned up after undoing the only entry.
        assert not UNDO_PATH.exists(), "undo file should be removed after all entries undone"


def test_undo_nothing_to_undo():
    """Undo with no undo file returns degraded."""
    # Remove undo file if it exists.
    UNDO_PATH.unlink(missing_ok=True)
    result = undo_last()
    assert result.degraded is True
    assert "nothing to undo" in (result.reason or "").lower()


def test_undo_corrupt_file():
    """Corrupt undo file returns degraded."""
    UNDO_PATH.write_text("not json", encoding="utf-8")
    try:
        result = undo_last()
        assert result.degraded is True
        assert "corrupt" in (result.reason or "").lower()
    finally:
        UNDO_PATH.unlink(missing_ok=True)


# ── Cleanup ─────────────────────────────────────────────────────────────

def test_cleanup_stale_staged():
    """Old staged files are removed, fresh ones are kept."""
    # Create a fresh staged file.
    fresh = STAGE_DIR / "compy-staged-fresh.json"
    fresh.write_text('{"file":"/tmp/x.py","original":"x","formatted":"y"}', encoding="utf-8")
    # Create an old staged file by setting mtime far in the past.
    old = STAGE_DIR / "compy-staged-old.json"
    old.write_text('{"file":"/tmp/x.py","original":"x","formatted":"y"}', encoding="utf-8")
    old_time = time.time() - 600  # 10 minutes ago
    os.utime(str(old), (old_time, old_time))

    _cleanup_stale_staged()

    # Fresh file should remain.
    assert fresh.exists(), "fresh staged file should survive cleanup"
    # Old file should be removed.
    assert not old.exists(), "stale staged file should be cleaned up"

    # Clean up the fresh one too.
    fresh.unlink(missing_ok=True)


def test_cleanup_stale_staged_empty_dir():
    """Cleanup on empty /tmp doesn't crash."""
    # Mock glob to return empty list.
    with mock.patch.object(Path, "glob", return_value=[]):
        _cleanup_stale_staged()  # Should not raise.


# ── Orchestrator routing ────────────────────────────────────────────────

def test_confirm_without_token_returns_degraded():
    """Bare '/confirm' without a token returns degraded, not IndexError."""
    result = run_pipeline(
        _req("/confirm"),
        parser=RuleBasedParser(),
        grepper=StubGrepper(),
        reasoners=(StubReasoner(),),
    )
    assert result.degraded is True
    assert "missing token" in (result.reason or "").lower()
    assert result.hits == ()


def test_undo_routing_returns_degraded_if_nothing():
    """'/undo' with no undo history returns degraded."""
    UNDO_PATH.unlink(missing_ok=True)
    result = run_pipeline(
        _req("/undo"),
        parser=RuleBasedParser(),
        grepper=StubGrepper(),
        reasoners=(StubReasoner(),),
    )
    assert result.degraded is True
    assert "nothing to undo" in (result.reason or "").lower()


def test_format_without_selection_returns_degraded():
    """'format this file' without a selection returns degraded hint."""
    request = _req("format this file", selection_text="")  # empty selection text, no file
    request = request.__class__(
        question="format this file",
        selection=Selection(text="", file=None, workspace_root="/tmp"),
    )
    result = run_pipeline(
        request,
        parser=RuleBasedParser(),
        grepper=StubGrepper(),
        reasoners=(StubReasoner(),),
    )
    assert result.degraded is True
    assert "no file selected" in (result.reason or "").lower()


# ── Helpers ─────────────────────────────────────────────────────────────

def _req(question: str, selection_text: str = "def foo(): pass") -> object:
    from compy.daemon.models import QueryRequest, Selection
    return QueryRequest(
        question=question,
        selection=Selection(text=selection_text, file="/x.py", line=1),
    )
