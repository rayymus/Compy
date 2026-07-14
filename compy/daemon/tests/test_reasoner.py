"""Tests for Reasoner adapters.

Coverage:
  - StubReasoner deterministic scoring and `raises` injection.
  - FreebuffReasoner: real CLI call raises ReasonerUnavailable (verified vs the locally
    installed freebuff v0.0.122 — no `-p` flag).
  - OllamaReasoner: real HTTP call raises ReasonerUnavailable when no server (verified
    in this env where ollama isn't installed).
"""

from __future__ import annotations

import pytest

from compy.daemon.interfaces import ReasonerUnavailable
from compy.daemon.models import GrepHit
from compy.daemon.reasoner import (
    FreebuffReasoner,
    OllamaReasoner,
    StubReasoner,
)


def _hit(file: str, line: int, text: str) -> GrepHit:
    return GrepHit(file=file, line=line, column=0, snippet=text)


# ---------- StubReasoner ---------------------------------------------------

def test_stub_default_scores_are_inverse_rank():
    stub = StubReasoner()
    out = stub.reason("q", (
        _hit("a.py", 1, "x"),
        _hit("b.py", 2, "y"),
        _hit("c.py", 3, "z"),
    ))
    assert [h.score for h in out] == [1.0, 0.5, pytest.approx(1 / 3)]


def test_stub_uses_explicit_scores_when_provided():
    stub = StubReasoner(scores=(0.9, 0.1, 0.05))
    out = stub.reason("q", (
        _hit("a.py", 1, "x"), _hit("b.py", 2, "y"), _hit("c.py", 3, "z"),
    ))
    assert [h.score for h in out] == [0.9, 0.1, 0.05]


def test_stub_records_calls_for_inspection():
    stub = StubReasoner()
    stub.reason("where?", (_hit("a.py", 1, "def foo(): pass"),))
    assert stub.calls == [("where?", (_hit("a.py", 1, "def foo(): pass"),))]


def test_stub_raises_when_injected():
    stub = StubReasoner(raises=True)
    with pytest.raises(ReasonerUnavailable):
        stub.reason("q", (_hit("a.py", 1, "x"),))


# ---------- FreebuffReasoner ---------------------------------------------

def test_freebuff_raises_when_cli_rejects_prompt_flag():
    """Honest integration: the installed freebuff v0.0.122 has no `-p` flag.

    The adapter surfaces this as ReasonerUnavailable so the orchestrator's fallback chain
    moves on to Ollama (and then to Stub). When wrapping is added later, this test will start
    failing — that's the signal to remove the wrapping from this adapter.
    """
    r = FreebuffReasoner(freebuff_path="freebuff", timeout_s=5.0)
    with pytest.raises(ReasonerUnavailable) as exc_info:
        r.reason("where is foo?", (_hit("a.py", 1, "def foo(): pass"),))
    msg = str(exc_info.value)
    # Either "exited non-zero" (rg the unknown flag) or similar — both forms indicate the
    # CLI doesn't accept the prompt mode we're trying to drive.
    assert "freebuff" in msg


# ---------- OllamaReasoner -------------------------------------------------

def test_ollama_raises_when_server_unreachable():
    """No ollama on this dev env — adapter must raise ReasonerUnavailable, not crash.

    Per DEVLOG: this test is designed for environments where Ollama is NOT running.
    When Ollama IS running (production machine), the test is skipped — the real HTTP
    call would succeed or fail differently depending on model availability.
    """
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1)
        pytest.skip("Ollama server is reachable — skipping unreachable test")
    except (urllib.error.URLError, OSError):
        pass  # Expected: server not running, proceed with test.

    r = OllamaReasoner(base_url="http://localhost:11434", timeout_s=2.0)
    with pytest.raises(ReasonerUnavailable) as exc_info:
        r.reason("where is foo?", (_hit("a.py", 1, "def foo(): pass"),))
    assert "ollama" in str(exc_info.value)


# ---------- Index response parsing ----------------------------------------

def test_indexed_response_parsing_orders_correctly():
    """`_interpret_indexed_response` (private but tested directly) extracts bracket indices."""
    from compy.daemon.reasoner import _interpret_indexed_response
    candidates = (
        _hit("a.py", 1, "x"), _hit("b.py", 2, "y"), _hit("c.py", 3, "z"),
    )
    # Mixed-up order in response: best order is [2, 0, 1]
    out = _interpret_indexed_response("best is [2]\nthen [0]\nthen [1]\n", candidates, source="x")
    assert [h.file for h in out] == ["c.py", "a.py", "b.py"]


def test_indexed_response_falls_back_to_input_order_when_unparseable():
    from compy.daemon.reasoner import _interpret_indexed_response
    candidates = (_hit("a.py", 1, "x"), _hit("b.py", 2, "y"))
    out = _interpret_indexed_response("no clear ranking here", candidates, source="x")
    assert [h.file for h in out] == ["a.py", "b.py"]
    assert out[0].score == 1.0
    assert out[1].score == 0.5
