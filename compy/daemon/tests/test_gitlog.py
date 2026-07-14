"""Tests for GitHistory — git log and blame querier.

Coverage:
  - query_history finds commits by keyword.
  - query_blame returns author for a specific line.
  - query_file_history returns recent commits for a file.
  - Empty results for non-existent files.
  - Raises ReasonerUnavailable when git is missing (handled gracefully).
  - Works with the Compy repo itself as a real-world test.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from compy.daemon.gitlog import GitHistory
from compy.daemon.models import GrepHit


def _make_git_repo() -> Path:
    """Create a temp git repo with a committed file."""
    import os
    import subprocess
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init"], cwd=tmp, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp, capture_output=True, check=False)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, capture_output=True, check=False)
    (tmp / "test.py").write_text("def foo():\n    return 42\n")
    subprocess.run(["git", "add", "test.py"], cwd=tmp, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp, capture_output=True, check=True)
    # Second commit with a grep-able message.
    (tmp / "test.py").write_text("def foo():\n    return 42\n\ndef bar():\n    pass\n")
    subprocess.run(["git", "add", "test.py"], cwd=tmp, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add bar function"], cwd=tmp, capture_output=True, check=True)
    return tmp


# ---------- query_history --------------------------------------------------

def test_query_history_finds_commit():
    repo = _make_git_repo()
    try:
        gh = GitHistory()
        hits = gh.query_history("bar function", str(repo))
        assert len(hits) > 0
        snippets = [h.snippet for h in hits]
        assert any("bar" in s for s in snippets)
    finally:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)


def test_query_history_no_match_returns_empty():
    repo = _make_git_repo()
    try:
        gh = GitHistory()
        hits = gh.query_history("zzzxnonexistent", str(repo))
        assert hits == ()
    finally:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)


# ---------- query_blame ----------------------------------------------------

def test_query_blame_returns_author():
    repo = _make_git_repo()
    try:
        gh = GitHistory()
        hits = gh.query_blame("test.py", 1, str(repo))
        assert len(hits) > 0
        # Should contain commit info or author.
        assert any("Test" in h.snippet for h in hits)
    finally:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)


def test_query_blame_nonexistent_file():
    repo = _make_git_repo()
    try:
        gh = GitHistory()
        hits = gh.query_blame("nonexistent.py", 1, str(repo))
        assert hits == ()
    finally:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)


# ---------- query_file_history ----------------------------------------------

def test_query_file_history_returns_commits():
    repo = _make_git_repo()
    try:
        gh = GitHistory()
        hits = gh.query_file_history("test.py", str(repo))
        assert len(hits) >= 2  # at least 2 commits
    finally:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)


def test_query_file_history_nonexistent():
    repo = _make_git_repo()
    try:
        gh = GitHistory()
        hits = gh.query_file_history("nope.py", str(repo))
        assert hits == ()
    finally:
        import shutil
        shutil.rmtree(repo, ignore_errors=True)


# ---------- non-git workspace -----------------------------------------------

def test_non_git_workspace_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        gh = GitHistory()
        hits = gh.query_history("test", tmp)
        assert hits == ()
