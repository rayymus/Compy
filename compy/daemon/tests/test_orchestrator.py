"""End-to-end pipeline tests using stubbed Parser / Grepper / Reasoner.

Coverage:
  - Happy path: parse -> grep -> rank.
  - Direct-hit short-circuit (≤3 hits, no Reasoner call).
  - Degraded path: all Reasoners fail; grep-only hits surfaced with degraded=True.
  - No-match path: empty results, degraded=False.
  - Confidence-below-threshold path: structural intent drops into fuzzy.
  - Reasoner fallback chain: skips reasoners that raise, picks the first that succeeds.
  - Reasoner returning empty-tuple falls through to the next reasoner.
  - Empty question: returns early.

Test data conventions:
  - All canned grep hits MUST contain `get_ability` in their snippet text so StubGrepper's
    substring filter matches against the parsed-symbol pattern.
"""

from __future__ import annotations

from compy.daemon.grepper import StubGrepper
from compy.daemon.interfaces import ReasonerUnavailable
from compy.daemon.models import GrepHit, QueryRequest, Selection
from compy.daemon.parser import RuleBasedParser
from compy.daemon.reasoner import StubReasoner
from compy.daemon.orchestrator import run as run_pipeline


def _hit(file: str, line: int, text: str) -> GrepHit:
    return GrepHit(file=file, line=line, column=0, snippet=text)


def _req(question: str, selection_text: str = "def get_ability(self):\n    return self._ability"):
    return QueryRequest(
        question=question,
        selection=Selection(text=selection_text, file="/x.py", line=1),
    )


# Sentinel hits that all contain "get_ability" so StubGrepper's substring filter matches.
_2_HITS = (
    _hit("/a.py", 10, "self.get_ability = 1"),
    _hit("/b.py", 7, "return self.get_ability()"),
)
_4_HITS = (
    _hit("/a.py", 10, "self.get_ability = 1"),
    _hit("/b.py", 7, "return self.get_ability()"),
    _hit("/c.py", 4, "via super().get_ability()"),
    _hit("/d.py", 9, "old_ref = get_ability # alias"),
)


# ---------- happy path -----------------------------------------------------

def test_happy_path_references_run_full_pipeline():
    """parse -> grep -> reasoner; many hits force the reasoner path."""
    g = StubGrepper(_4_HITS)
    r = StubReasoner()

    result = run_pipeline(
        _req("where else is get_ability used?"),
        parser=RuleBasedParser(),
        grepper=g,
        reasoners=(r,),
    )
    assert result.intent == "references"
    assert len(result.hits) == 4
    assert result.degraded is False
    assert all(h.source == "stub" for h in result.hits)
    assert len(r.calls) == 1


def test_direct_hit_short_circuits_reasoner():
    """≤3 hits in the structural case → no Reasoner call, hits promoted as direct."""
    g = StubGrepper(_2_HITS)
    r = StubReasoner()

    result = run_pipeline(
        _req("where else is get_ability used?"),
        parser=RuleBasedParser(), grepper=g, reasoners=(r,),
    )
    assert len(result.hits) == 2
    assert result.degraded is False
    assert r.calls == []
    assert all(h.source == "grep" for h in result.hits)
    assert all(h.score == 1.0 for h in result.hits)


# ---------- degraded ------------------------------------------------------

def test_degraded_returns_grep_hits_when_all_reasoners_fail():
    """All reasoners raise → grep-only hits, degraded=True, reason set."""
    g = StubGrepper(_4_HITS)
    failing_a = StubReasoner(raises=True)
    failing_b = StubReasoner(raises=True)

    result = run_pipeline(
        _req("where else is get_ability used?"),
        parser=RuleBasedParser(), grepper=g, reasoners=(failing_a, failing_b),
    )
    assert result.degraded is True
    assert "reasoners unavailable" in (result.reason or "")
    assert len(result.hits) == 4
    assert all(h.source == "grep" for h in result.hits)
    assert len(failing_a.calls) == 1
    assert len(failing_b.calls) == 1


def test_reasoner_fallback_picks_first_that_succeeds():
    """First reasoner raises; second succeeds; orchestrator returns second's ranking."""
    g = StubGrepper(_4_HITS)
    failing = StubReasoner(raises=True)
    succeeding = StubReasoner()

    result = run_pipeline(
        _req("where else is get_ability used?"),
        parser=RuleBasedParser(), grepper=g,
        reasoners=(failing, succeeding),
    )
    assert result.degraded is False
    assert all(h.source == "stub" for h in result.hits)
    assert len(failing.calls) == 1
    assert len(succeeding.calls) == 1


def test_reasoner_returning_empty_results_falls_through():
    """A reasoner that returns `()` (rather than raising) must fall through.

    Coverage for the `if ranked: ... last_err = 'returned empty'` branch in the orchestrator.
    """
    g = StubGrepper(_4_HITS)
    empty_reasoner = StubReasoner(empty_returns=True)
    succeeding = StubReasoner()

    result = run_pipeline(
        _req("where else is get_ability used?"),
        parser=RuleBasedParser(), grepper=g,
        reasoners=(empty_reasoner, succeeding),
    )
    assert len(empty_reasoner.calls) == 1
    assert len(succeeding.calls) == 1
    assert result.degraded is False
    assert all(h.source == "stub" for h in result.hits)


# ---------- no-match -------------------------------------------------------

def test_no_matches_returns_empty_result():
    g = StubGrepper()
    r = StubReasoner()

    result = run_pipeline(
        QueryRequest(question="anything"),
        parser=RuleBasedParser(), grepper=g, reasoners=(r,),
    )
    assert result.hits == ()
    assert result.degraded is False
    assert result.reason == "no hits"
    assert r.calls == []


# ---------- fuzzy branch ---------------------------------------------------

def test_confidence_below_threshold_drops_into_fuzzy():
    """Selection-less, generic question → fuzzy branch → keyword grep retries."""
    parse_hit = _hit("/x.py", 5, "def parse_query(): pass")
    g = StubGrepper((parse_hit,))

    request = QueryRequest(question="how does parsing work")
    result = run_pipeline(
        request,
        parser=RuleBasedParser(), grepper=g, reasoners=(StubReasoner(),),
    )
    patterns_tried = [p for p, _ in g.calls]
    assert patterns_tried
    assert any(p in {"how", "parsing", "work"} for p in patterns_tried)# ---------- workspace-root None regression -------------------------------

def test_selection_with_none_workspace_root_defaults_to_dot():
    """Selection exists but workspace_root is None (e.g. clipboard-swap without
    extension JSON). The orchestrator must default to '.' instead of passing
    None to ripgrep (which causes subprocess TypeError)."""
    g = StubGrepper(_2_HITS)
    request = QueryRequest(
        question="where else is get_ability used?",
        selection=Selection(text="get_ability", file="/x.py", line=1, workspace_root=None),
    )
    result = run_pipeline(
        request,
        parser=RuleBasedParser(), grepper=g, reasoners=(StubReasoner(),),
    )
    # Must not crash — workspace_root=None should be treated as "."
    assert result.degraded is False
    assert len(result.hits) == 2
    # StubGrepper records calls — workspace should be "." not None
    assert g.calls
    for _, ws in g.calls:
        assert ws == ".", f"expected workspace '.', got {ws!r}"


# ---------- guard ---------------------------------------------------------
def test_empty_question_returns_empty():
    g = StubGrepper()
    r = StubReasoner()
    result = run_pipeline(
        QueryRequest(question="   "),
        parser=RuleBasedParser(), grepper=g, reasoners=(r,),
    )
    assert result.intent == "empty"
    assert result.hits == ()
    assert g.calls == []
    assert r.calls == []
