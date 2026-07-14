"""Tests for the Grepper surface.

Coverage:
  - StubGrepper filters canned hits by substring pattern (deterministic).
  - RipgrepGrepper real run against a temp dir of mixed file types.
  - RipgrepGrepper raises ReasonerUnavailable on a broken rg path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from compy.daemon.grepper import RipgrepGrepper, StubGrepper
from compy.daemon.interfaces import ReasonerUnavailable
from compy.daemon.models import GrepHit


# ---------- StubGrepper ----------------------------------------------------

def test_stub_returns_filtered_hits():
    stub = StubGrepper((
        GrepHit("a.py", 1, 0, "def foo(self): pass"),
        GrepHit("b.py", 2, 0, "def bar(self): pass"),
    ))
    out = stub.grep("foo", "/any")
    assert len(out) == 1
    assert out[0].file == "a.py"


def test_stub_records_calls():
    stub = StubGrepper()
    stub.grep("foo", "/some/where")
    stub.grep("bar", "/other")
    assert stub.calls == [("foo", "/some/where"), ("bar", "/other")]


# ---------- RipgrepGrepper -------------------------------------------------

# Skip the real-rg tests if ripgrep isn't installed (e.g. CI without rg).
RG = shutil.which("rg")
pytestmark_real = pytest.mark.skipif(RG is None, reason="ripgrep not installed")


@pytestmark_real
def test_ripgrep_finds_hits_in_temp_py_repo(tmp_path: Path):
    (tmp_path / "a.py").write_text("def foo(self):\n    return 1\n")
    (tmp_path / "b.py").write_text("def bar(self):\n    return foo() + 1\n")
    (tmp_path / "c.txt").write_text("def foo(): pass\n")

    rg = RipgrepGrepper(rg_path=RG or "rg")
    hits = rg.grep("foo", str(tmp_path))
    # .txt is no longer in the denylist — all three files should appear.
    filenames = {Path(h.file).name for h in hits}
    assert filenames == {"a.py", "b.py", "c.txt"}


@pytestmark_real
def test_ripgrep_denylist_filters_minified_js(tmp_path: Path):
    """Compiled/minified files should still be excluded."""
    (tmp_path / "src.js").write_text("function foo() { return 1; }\n")
    (tmp_path / "src.min.js").write_text("function foo(){return 1;}\n")

    rg = RipgrepGrepper(rg_path=RG or "rg")
    hits = rg.grep("foo", str(tmp_path))
    filenames = {Path(h.file).name for h in hits}
    assert "src.js" in filenames
    assert "src.min.js" not in filenames  # .min.js is in denylist


@pytestmark_real
def test_ripgrep_zero_matches_returns_empty(tmp_path: Path):
    (tmp_path / "a.py").write_text("def nothing_to_see():\n    return 1\n")

    rg = RipgrepGrepper(rg_path=RG or "rg")
    hits = rg.grep("xyzzy_no_match", str(tmp_path))
    assert hits == ()


def test_ripgrep_missing_binary_raises_reasoner_unavailable():
    rg = RipgrepGrepper(rg_path="/nonexistent/rg")
    with pytest.raises(ReasonerUnavailable) as exc_info:
        rg.grep("anything", "/")
    assert "not found" in str(exc_info.value)
