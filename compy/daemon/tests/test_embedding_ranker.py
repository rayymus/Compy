"""Tests for EmbeddingRanker — semantic similarity ranking.

Tests verify:
  - Cosine similarity computation correctness.
  - Fallback (ReasonerUnavailable) when Ollama is unreachable.
  - Ranking order matches embedding similarity.
  - Blend with token overlap works.
  - Empty candidates returns empty.
  - Implements the Reasoner Protocol.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from compy.daemon.embedding_ranker import EmbeddingRanker, _cosine
from compy.daemon.interfaces import ReasonerUnavailable
from compy.daemon.models import GrepHit


def _mock_response(payload: dict) -> bytes:
    """Build a fake urlopen response returning JSON payload."""
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    mock.read = MagicMock(return_value=json.dumps(payload).encode("utf-8"))
    return mock


def _mock_embedding(vec: list[float]) -> MagicMock:
    """Build a fake urlopen response returning an embedding."""
    return _mock_response({"embedding": vec})


def _hit(file: str, line: int, snippet: str, context: str | None = None) -> GrepHit:
    return GrepHit(file=file, line=line, column=0, snippet=snippet, context=context)


# ── Cosine similarity unit tests ──────────────────────────────────────────

def test_cosine_identical_vectors():
    """Identical vectors → 1.0."""
    assert _cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors():
    """Orthogonal vectors → 0.0."""
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors():
    """Opposite vectors → -1.0."""
    assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_empty_vectors():
    """Empty or mismatched-length vectors → 0.0."""
    assert _cosine([], [1.0]) == 0.0
    assert _cosine([1.0, 2.0], [1.0]) == 0.0


def test_cosine_zero_norm():
    """Zero-norm vector → 0.0."""
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# ── EmbeddingRanker Protocol + behavior ───────────────────────────────────

def test_embedding_ranker_implements_protocol():
    """EmbeddingRanker must have name property and reason method."""
    r = EmbeddingRanker()
    assert r.name == "embedding"
    assert hasattr(r, "reason")


def test_empty_candidates_returns_empty():
    """No candidates → empty tuple, no API calls."""
    r = EmbeddingRanker()
    result = r.reason("test", ())
    assert result == ()


def test_raises_when_ollama_unreachable():
    """Ollama down → ReasonerUnavailable so chain falls through."""
    with patch("urllib.request.urlopen", side_effect=OSError("refused")):
        r = EmbeddingRanker(timeout_s=0.5)
        with pytest.raises(ReasonerUnavailable):
            r.reason("where is auth?", (_hit("a.py", 1, "def auth(): pass"),))


def test_ranks_by_semantic_similarity():
    """Candidate with higher embedding similarity should rank first."""
    # Query embedding is close to candidate 0, far from candidate 1.
    q_vec = [1.0, 0.0, 0.0]
    close_vec = [0.9, 0.1, 0.0]   # cosine ~0.994
    far_vec = [0.0, 0.1, 0.9]     # cosine ~0.0

    call_count = [0]
    def mock_urlopen(req, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return _mock_embedding(q_vec)       # question embedding
        elif call_count[0] == 2:
            return _mock_embedding(close_vec)   # first candidate
        else:
            return _mock_embedding(far_vec)     # second candidate

    candidates = (
        _hit("auth.py", 10, "def authenticate(user): ..."),
        _hit("models.py", 20, "class Database: ..."),
    )
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        r = EmbeddingRanker(blend=1.0)  # pure embedding, no token overlap
        result = r.reason("authentication logic", candidates)

    assert len(result) == 2
    # The semantically close candidate should be first.
    assert result[0].file == "auth.py"
    assert result[0].score >= result[1].score
    assert result[0].source == "embedding"


def test_structural_context_preserved():
    """Context from GrepHit should propagate to RankedHit."""
    q_vec = [1.0, 0.0]
    c_vec = [1.0, 0.0]

    call_count = [0]
    def mock_urlopen(req, timeout=None):
        call_count[0] += 1
        return _mock_embedding(q_vec if call_count[0] == 1 else c_vec)

    candidates = (
        _hit("auth.py", 10, "def auth():", context="Called by: login, main"),
    )
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        r = EmbeddingRanker(blend=1.0)
        result = r.reason("auth", candidates)

    assert result[0].structural_context == "Called by: login, main"


def test_all_zero_scores_returns_empty():
    """When all candidates score 0, return empty so chain falls through."""
    # Orthogonal vectors → cosine 0 for all.
    q_vec = [1.0, 0.0]
    c_vec = [0.0, 1.0]

    call_count = [0]
    def mock_urlopen(req, timeout=None):
        call_count[0] += 1
        return _mock_embedding(q_vec if call_count[0] == 1 else c_vec)

    candidates = (_hit("a.py", 1, "unrelated code"),)
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        r = EmbeddingRanker(blend=1.0)
        result = r.reason("completely different", candidates)

    assert result == ()


def test_blend_with_token_overlap():
    """Blended score should incorporate both embedding and token overlap."""
    # Query: "authenticate user"
    # Candidate A: "def authenticate(user): pass" — high token overlap, high embedding
    # Candidate B: "def login(session): pass" — low token overlap, low embedding
    q_vec = [1.0, 0.0]
    a_vec = [0.95, 0.05]  # close to query
    b_vec = [0.3, 0.7]    # somewhat far

    call_count = [0]
    embeddings = [q_vec, a_vec, b_vec]
    def mock_urlopen(req, timeout=None):
        idx = min(call_count[0], len(embeddings) - 1)
        call_count[0] += 1
        return _mock_embedding(embeddings[idx])

    candidates = (
        _hit("auth.py", 10, "def authenticate(user): pass"),
        _hit("login.py", 5, "def login(session): pass"),
    )
    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        r = EmbeddingRanker(blend=0.6)
        result = r.reason("authenticate user", candidates)

    # Candidate A should win — higher on both axes.
    assert result[0].file == "auth.py"
    assert result[0].score > result[1].score
    # Scores should be normalized (top = 1.0).
    assert result[0].score == pytest.approx(1.0, abs=0.01)


def test_fallback_on_malformed_embedding():
    """Malformed embedding response → ReasonerUnavailable."""
    bad_response = _mock_response({"error": "model not found"})
    with patch("urllib.request.urlopen", return_value=bad_response):
        r = EmbeddingRanker(timeout_s=0.5)
        with pytest.raises(ReasonerUnavailable):
            r.reason("test", (_hit("a.py", 1, "code"),))
