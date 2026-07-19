"""Tests for the Working Set Engine — Session 34.

Covers: decay, Personalized PageRank bias, topic-shift reset, cold start,
persistence round-trip, click feedback, next-question generation.
"""
from __future__ import annotations

import json

import pytest

from compy.daemon.models import RankedHit
from compy.daemon.workingset import (
    BIAS_BLEND,
    CLICK_BOOST,
    DECAY_FACTOR,
    MIN_SCORE,
    NEIGHBOR_BOOST,
    QUERY_BOOST,
    WorkingSet,
    _click_path,
    _norm_file,
    _ws_path,
)


# ─── Helpers ───────────────────────────────────────────────────────────────

def _hit(file: str, line: int, score: float = 0.5) -> RankedHit:
    """Build a minimal RankedHit for testing."""
    return RankedHit(
        file=file,
        line=line,
        snippet=f"def func_{line}(): pass",
        score=score,
        source="heuristic",
    )


def _make_graph():
    """Build a small DiGraph: A → B → C, A → D."""
    import networkx as nx
    g = nx.DiGraph()
    g.add_node("src/auth.py::login", kind="function", file="src/auth.py", line=10, language="python")
    g.add_node("src/auth.py::validate", kind="function", file="src/auth.py", line=30, language="python")
    g.add_node("src/session.py::create", kind="function", file="src/session.py", line=5, language="python")
    g.add_node("src/utils.py::hash", kind="function", file="src/utils.py", line=15, language="python")
    g.add_edge("src/auth.py::login", "src/auth.py::validate")   # login calls validate
    g.add_edge("src/auth.py::login", "src/session.py::create")  # login calls create
    g.add_edge("src/auth.py::validate", "src/utils.py::hash")   # validate calls hash
    return g


WORKSPACE = "/tmp/test-compy-ws"


@pytest.fixture(autouse=True)
def _cleanup_tmp():
    """Remove working set tmp files before and after each test."""
    for p in [_ws_path(WORKSPACE), _click_path(WORKSPACE)]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    yield
    for p in [_ws_path(WORKSPACE), _click_path(WORKSPACE)]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


# ─── Cold Start ────────────────────────────────────────────────────────────

def test_cold_start_has_no_activation():
    """First query: no activation scores, bias_hits returns hits unchanged."""
    ws = WorkingSet.load(WORKSPACE)
    assert not ws.has_activation
    hits = (_hit("src/auth.py", 10, 0.9), _hit("src/session.py", 5, 0.7))
    biased, personalized = ws.bias_hits(hits, graph=None)
    assert biased == hits  # unchanged
    assert personalized is False


def test_cold_start_next_questions_empty():
    """No activation → no next questions to suggest."""
    ws = WorkingSet.load(WORKSPACE)
    assert ws.generate_next_questions(graph=None) == []


# ─── Decay ─────────────────────────────────────────────────────────────────

def test_decay_reduces_scores():
    """Decay multiplies all scores by DECAY_FACTOR."""
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 1.0, "src/session.py:5": 0.5}
    ws.decay()
    assert ws._activation["src/auth.py:10"] == pytest.approx(1.0 * DECAY_FACTOR)
    assert ws._activation["src/session.py:5"] == pytest.approx(0.5 * DECAY_FACTOR)


def test_decay_prunes_below_min_score():
    """Scores below MIN_SCORE after decay are pruned."""
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": MIN_SCORE / DECAY_FACTOR * 0.9}  # will decay below MIN_SCORE
    ws.decay()
    assert "src/auth.py:10" not in ws._activation


def test_decay_multiple_turns_fades_scores():
    """After 5 turns, a score of 1.0 fades to DECAY_FACTOR^5."""
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 1.0}
    for _ in range(5):
        ws.decay()
    expected = DECAY_FACTOR ** 5
    assert ws._activation["src/auth.py:10"] == pytest.approx(expected, rel=0.01)


# ─── Topic-Shift Detection ─────────────────────────────────────────────────

def test_topic_shift_detected_on_low_overlap():
    """Completely different keywords → shift detected."""
    ws = WorkingSet(WORKSPACE)
    ws.record_keywords(("auth", "login", "session"))
    assert ws.detect_topic_shift(("payment", "stripe", "charge")) is True


def test_no_topic_shift_on_related_keywords():
    """Overlapping keywords → no shift."""
    ws = WorkingSet(WORKSPACE)
    ws.record_keywords(("auth", "login", "session"))
    assert ws.detect_topic_shift(("auth", "password", "session")) is False


def test_topic_shift_threshold():
    """Overlap exactly at threshold → no shift (>= threshold is not a shift)."""
    ws = WorkingSet(WORKSPACE)
    ws.record_keywords(("a", "b", "c", "d"))
    # New keywords: 1 overlap ("a"), 7 total → 1/7 ≈ 0.143 < 0.15 → shift
    assert ws.detect_topic_shift(("a", "e", "f", "g")) is True
    # New keywords: 2 overlap ("a", "b"), 6 total → 2/6 ≈ 0.33 > 0.15 → no shift
    assert ws.detect_topic_shift(("a", "b", "e", "f")) is False


def test_topic_shift_reset_clears_activation():
    """After a topic shift + reset, activation is empty."""
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 1.0}
    ws._recent_keywords = ["auth", "login"]
    ws.reset()
    assert ws._activation == {}
    assert ws._recent_keywords == []


def test_first_query_no_topic_shift():
    """First query (no recent keywords) → never a shift."""
    ws = WorkingSet(WORKSPACE)
    assert ws.detect_topic_shift(("anything", "here")) is False


# ─── Query Recording ───────────────────────────────────────────────────────

def test_record_query_boosts_all_hits():
    """Each result hit gets QUERY_BOOST added to its activation."""
    ws = WorkingSet(WORKSPACE)
    hits = (_hit("src/auth.py", 10), _hit("src/session.py", 5))
    ws.record_query(hits)
    assert ws._activation["src/auth.py:10"] == pytest.approx(QUERY_BOOST)
    assert ws._activation["src/session.py:5"] == pytest.approx(QUERY_BOOST)
    assert ws.turn_count == 1


def test_record_query_accumulates():
    """Multiple queries accumulate activation scores."""
    ws = WorkingSet(WORKSPACE)
    hits = (_hit("src/auth.py", 10),)
    ws.record_query(hits)
    ws.record_query(hits)
    assert ws._activation["src/auth.py:10"] == pytest.approx(QUERY_BOOST * 2)


# ─── Click Feedback ────────────────────────────────────────────────────────

def test_consume_click_boosts_clicked_node():
    """A click signal file boosts the clicked node's activation."""
    ws = WorkingSet(WORKSPACE)
    _click_path(WORKSPACE).write_text(
        json.dumps({"file": "src/auth.py", "line": 10}), "utf-8"
    )
    ws.consume_click(graph=None)
    assert ws._activation["src/auth.py:10"] == pytest.approx(CLICK_BOOST)


def test_consume_click_deletes_signal_file():
    """After consuming a click, the signal file is removed."""
    ws = WorkingSet(WORKSPACE)
    _click_path(WORKSPACE).write_text(
        json.dumps({"file": "src/auth.py", "line": 10}), "utf-8"
    )
    ws.consume_click(graph=None)
    assert not _click_path(WORKSPACE).exists()


def test_consume_click_no_file_is_noop():
    """No click file → consume_click is a no-op (no crash)."""
    ws = WorkingSet(WORKSPACE)
    ws.consume_click(graph=None)
    assert ws._activation == {}


def test_consume_click_malformed_file_is_noop():
    """Malformed click file → consume_click skips silently (no crash)."""
    ws = WorkingSet(WORKSPACE)
    _click_path(WORKSPACE).write_text("not valid json", "utf-8")
    ws.consume_click(graph=None)
    assert ws._activation == {}


def test_consume_click_with_graph_propagates_to_neighbors():
    """Click with graph → callers and callees get NEIGHBOR_BOOST."""
    g = _make_graph()
    ws = WorkingSet(WORKSPACE)
    _click_path(WORKSPACE).write_text(
        json.dumps({"file": "src/auth.py", "line": 10}), "utf-8"
    )
    ws.consume_click(graph=g)
    # Clicked node
    assert ws._activation["src/auth.py:10"] == pytest.approx(CLICK_BOOST)
    # Callers of login: none (login is the root in our graph)
    # Callees of login: validate (line 30) and create (line 5)
    assert ws._activation["src/auth.py:30"] == pytest.approx(NEIGHBOR_BOOST)
    assert ws._activation["src/session.py:5"] == pytest.approx(NEIGHBOR_BOOST)
    # 2-hop neighbor (hash) should NOT be boosted — propagation is 1-hop only.
    assert "src/utils.py:15" not in ws._activation


# ─── Bias ──────────────────────────────────────────────────────────────────

def test_bias_raw_without_graph():
    """Without a graph, bias uses raw activation scores."""
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 1.0}
    hits = (_hit("src/auth.py", 10, 0.5), _hit("src/session.py", 5, 0.8))
    biased, personalized = ws.bias_hits(hits, graph=None)
    assert personalized is True
    # The active hit (auth:10) gets boosted, the inactive one doesn't
    auth_score = next(h for h in biased if h.file == "src/auth.py").score
    session_score = next(h for h in biased if h.file == "src/session.py").score
    # auth: 0.5 * (1-BIAS_BLEND) + 1.0 * BIAS_BLEND
    assert auth_score == pytest.approx(0.5 * (1 - BIAS_BLEND) + 1.0 * BIAS_BLEND)
    # session: unchanged (no activation)
    assert session_score == pytest.approx(0.8)


def test_bias_with_graph_uses_pagerank():
    """With a graph, bias uses Personalized PageRank."""
    g = _make_graph()
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 1.0}
    hits = (_hit("src/auth.py", 10, 0.5), _hit("src/utils.py", 15, 0.9))
    biased, personalized = ws.bias_hits(hits, graph=g)
    assert personalized is True
    # The active hit (auth:10, which is the seed) should be boosted
    # The non-active hit (utils:15) might get a small PR boost via propagation
    # but the key check is that bias happened and ordering may change
    assert len(biased) == 2
    # Both scores should be in valid range
    for h in biased:
        assert 0.0 <= h.score <= 1.0


def test_bias_preserves_score_range():
    """Biased scores never exceed 1.0 or go below 0.0."""
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 10.0}  # very high activation
    hits = (_hit("src/auth.py", 10, 1.0),)
    biased, _ = ws.bias_hits(hits, graph=None)
    assert biased[0].score <= 1.0


def test_bias_reorders_hits():
    """A lower-ranked hit with high activation can overtake a higher-ranked one."""
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 1.0}
    hits = (
        _hit("src/session.py", 5, 0.8),  # higher original score, no activation
        _hit("src/auth.py", 10, 0.3),    # lower original score, high activation
    )
    biased, personalized = ws.bias_hits(hits, graph=None)
    assert personalized is True
    # After bias: auth = 0.3*0.8 + 1.0*0.2 = 0.44, session = 0.8 (unchanged — no activation)
    # Session still wins because the gap was too large.
    # But with a smaller gap:
    ws2 = WorkingSet(WORKSPACE)
    ws2._activation = {"src/auth.py:10": 1.0}
    hits2 = (
        _hit("src/session.py", 5, 0.4),  # smaller gap
        _hit("src/auth.py", 10, 0.3),
    )
    biased2, _ = ws2.bias_hits(hits2, graph=None)
    # auth: 0.3*0.8 + 0.2 = 0.44, session: 0.4 (unchanged — no activation) → auth wins
    assert biased2[0].file == "src/auth.py"


# ─── Persistence ───────────────────────────────────────────────────────────

def test_persistence_round_trip():
    """Save → load preserves activation scores and keywords."""
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 0.7, "src/session.py:5": 0.3}
    ws._recent_keywords = ["auth", "login"]
    ws._turn_count = 3
    ws.save()

    ws2 = WorkingSet.load(WORKSPACE)
    assert ws2._activation["src/auth.py:10"] == pytest.approx(0.7)
    assert ws2._activation["src/session.py:5"] == pytest.approx(0.3)
    assert ws2._recent_keywords == ["auth", "login"]
    assert ws2.turn_count == 3


def test_persistence_survives_missing_file():
    """Loading when no tmp file exists → cold start (no crash)."""
    # _cleanup_tmp fixture already removed the file
    ws = WorkingSet.load(WORKSPACE)
    assert ws._activation == {}
    assert ws.turn_count == 0


def test_persistence_survives_corrupt_file():
    """Loading a corrupt tmp file → cold start (no crash)."""
    _ws_path(WORKSPACE).write_text("not valid json {{{", "utf-8")
    ws = WorkingSet.load(WORKSPACE)
    assert ws._activation == {}
    assert ws.turn_count == 0


def test_workspace_normalization():
    """Workspace with trailing slash produces same tmp path as without."""
    ws1 = WorkingSet("/tmp/test-compy-ws")
    ws2 = WorkingSet("/tmp/test-compy-ws/")
    # After normpath, both should have the same workspace
    assert ws1.workspace == ws2.workspace


# ─── Next-Question Generation ──────────────────────────────────────────────

def test_next_questions_from_active_nodes():
    """Active nodes with callers generate 'X is called in N places' suggestions."""
    g = _make_graph()
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 1.0}  # login node
    questions = ws.generate_next_questions(graph=g)
    # login has 0 callers (it's the root), 2 callees (validate, create)
    # So the first question should be "Is login still used?" (0 callers)
    # And possibly "What does login call?" (>3 callees is false, 2 callees)
    assert len(questions) > 0
    assert any("login" in q for q in questions)


def test_next_questions_empty_without_graph():
    """No graph → no next questions."""
    ws = WorkingSet(WORKSPACE)
    ws._activation = {"src/auth.py:10": 1.0}
    assert ws.generate_next_questions(graph=None) == []


def test_next_questions_empty_without_activation():
    """No activation → no next questions."""
    g = _make_graph()
    ws = WorkingSet(WORKSPACE)
    assert ws.generate_next_questions(graph=g) == []


def test_next_questions_max_three():
    """At most 3 next questions are generated."""
    g = _make_graph()
    ws = WorkingSet(WORKSPACE)
    # Set activation on multiple nodes
    ws._activation = {
        "src/auth.py:10": 1.0,
        "src/auth.py:30": 0.8,
        "src/session.py:5": 0.6,
        "src/utils.py:15": 0.4,
    }
    questions = ws.generate_next_questions(graph=g)
    assert len(questions) <= 3


# ─── File Normalization ────────────────────────────────────────────────────

def test_norm_file_strips_dot_slash():
    """Relative paths with ./ prefix are normalized."""
    assert _norm_file("./src/auth.py", WORKSPACE) == "src/auth.py"


def test_norm_file_absolute_to_relative():
    """Absolute paths are converted to workspace-relative."""
    ws = WORKSPACE
    assert _norm_file(f"{ws}/src/auth.py", ws) == "src/auth.py"


# ─── Integration: Full Turn Cycle ──────────────────────────────────────────

def test_full_turn_cycle():
    """Simulate a 2-turn session: query → click → query with bias."""
    g = _make_graph()

    # Turn 1: cold start, no activation
    ws = WorkingSet.load(WORKSPACE)
    ws.consume_click(None)
    ws.decay()
    ws.record_keywords(("auth", "login"))
    hits1 = (_hit("src/auth.py", 10, 0.9), _hit("src/session.py", 5, 0.7))
    ws.record_query(hits1)
    ws.save()

    # Simulate a click on the first result
    _click_path(WORKSPACE).write_text(
        json.dumps({"file": "src/auth.py", "line": 10}), "utf-8"
    )

    # Turn 2: load, consume click, decay, bias
    ws2 = WorkingSet.load(WORKSPACE)
    assert ws2.has_activation  # from turn 1's record_query
    ws2.consume_click(g)  # click + neighbor propagation
    ws2.decay()  # decay everything
    ws2.record_keywords(("auth", "login"))
    hits2 = (_hit("src/auth.py", 10, 0.5), _hit("src/session.py", 5, 0.6))
    biased, personalized = ws2.bias_hits(hits2, graph=g)
    assert personalized is True
    # auth:10 should be boosted (it has high activation from click + query + decay)
    auth_biased = next(h for h in biased if h.file == "src/auth.py")
    assert auth_biased.score > 0.5  # boosted above original
