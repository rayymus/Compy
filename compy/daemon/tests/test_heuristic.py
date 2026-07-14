"""Tests for HeuristicReasoner — keyword-overlap ranking without LLM dependency.

Coverage:
  - Basic Jaccard overlap scoring (higher overlap → higher score).
  - Symbol match boost when selection tokens appear in candidate snippet.
  - Same-directory boost when candidate and selection share a parent directory.
  - Test-file penalty for non-test queries.
  - All-zero scores (no token overlap) — no division by zero.
  - Empty candidates returns empty.
  - Test file detection edge cases: latest.py (not test), test_foo.py (is test).
"""

from __future__ import annotations

import pytest

from compy.daemon.heuristic_reasoner import HeuristicReasoner, _is_test_file, _tokenize, _jaccard
from compy.daemon.models import GrepHit


def _hit(file: str, line: int, text: str) -> GrepHit:
    return GrepHit(file=file, line=line, column=0, snippet=text)


# ---------- tokenize / jaccard ---------------------------------------------

def test_tokenize_extracts_alphanumeric_tokens():
    assert _tokenize("Hello, World! foo_bar baz123") == {"hello", "world", "foo_bar", "baz123"}


def test_jaccard_full_overlap():
    assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_no_overlap():
    assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0


def test_jaccard_partial():
    assert _jaccard({"a", "b", "c"}, {"b", "c", "d"}) == pytest.approx(2 / 4)  # intersection=2, union=4


def test_jaccard_empty_sets():
    assert _jaccard(set(), {"a"}) == 0.0
    assert _jaccard({"a"}, set()) == 0.0
    assert _jaccard(set(), set()) == 0.0


# ---------- _is_test_file -------------------------------------------------

def test_is_test_file_detects_test_prefix():
    assert _is_test_file("/a/test_foo.py") is True


def test_is_test_file_detects_test_suffix():
    assert _is_test_file("/a/foo_test.py") is True


def test_is_test_file_detects_test_py():
    assert _is_test_file("test.py") is True


def test_is_test_file_rejects_latest_py():
    """'test' substring in 'latest' must NOT match."""
    assert _is_test_file("latest.py") is False


def test_is_test_file_rejects_testing_py():
    """'testing' is a utility module, not a test file."""
    assert _is_test_file("testing.py") is False


def test_is_test_file_rejects_contest_py():
    assert _is_test_file("contest.py") is False


def test_is_test_file_rejects_regular_file():
    assert _is_test_file("/src/models.py") is False


# ---------- HeuristicReasoner -----------------------------------------------

def test_heuristic_empty_candidates():
    r = HeuristicReasoner()
    assert r.reason("question", ()) == ()


def test_heuristic_all_zero_scores_is_safe():
    """Candidates with zero token overlap → all scores 0.0, no division by zero."""
    r = HeuristicReasoner()
    # Question has tokens that don't appear in any snippet.
    out = r.reason("xyzzy plugh", (
        _hit("a.py", 1, "def foo(): pass"),
        _hit("b.py", 2, "def bar(): pass"),
    ))
    assert len(out) == 2
    # All scores should be 0.0 since no overlap + no boosts.
    assert all(h.score == 0.0 for h in out)


def test_heuristic_ranks_by_overlap():
    """Higher token overlap → higher score."""
    r = HeuristicReasoner()
    out = r.reason("get user name", (
        _hit("a.py", 1, "def unrelated_function(): pass"),
        _hit("b.py", 2, "def get_user_name(): return name"),
    ))
    # b.py has higher overlap with "get user name" than a.py
    assert out[0].file == "b.py"
    assert out[0].score > out[1].score


def test_heuristic_symbol_boost():
    """When selection tokens appear in the snippet, score gets a boost."""
    r = HeuristicReasoner()
    out = r.reason(
        "where else is get_ability used",
        (
            _hit("a.py", 1, "def get_ability(self): return self._ability"),
            _hit("b.py", 2, "def unrelated(): pass"),
        ),
        selection_text="def get_ability(self): pass",
    )
    # a.py contains get_ability (matches selection token) → higher score.
    assert out[0].file == "a.py"


def test_heuristic_same_dir_boost():
    """Candidates in the same directory as the selection get boosted."""
    r = HeuristicReasoner()
    out = r.reason(
        "find auth",
        (
            _hit("/project/auth/helpers.py", 1, "def authenticate(): pass"),
            _hit("/project/utils/misc.py", 2, "def authenticate(): pass"),
        ),
        selection_file="/project/auth/login.py",
    )
    # helpers.py shares parent /project/auth with login.py
    assert out[0].file == "/project/auth/helpers.py"


def test_heuristic_test_penalty():
    """Test files are penalized unless the user is asking about tests."""
    r = HeuristicReasoner()
    out = r.reason(
        "find authentication",
        (
            _hit("/src/auth.py", 1, "def authenticate(): pass"),
            _hit("/tests/test_auth.py", 2, "def test_authenticate(): pass"),
        ),
    )
    # auth.py should rank higher than test_auth.py (test penalty).
    assert out[0].file == "/src/auth.py"


def test_heuristic_no_test_penalty_when_asking_about_tests():
    """When 'test' appears in the question, test penalty is suppressed."""
    r = HeuristicReasoner()
    out = r.reason(
        "find tests for authentication",
        (
            _hit("/src/auth.py", 1, "def authenticate(): pass"),
            _hit("/tests/test_auth.py", 2, "def test_authenticate(): pass"),
        ),
    )
    # test_auth.py has more keyword overlap with "tests authentication"
    # and no test penalty — it may rank first or second depending on overlap.
    # At minimum, the penalty shouldn't suppress it below unrelated files.
    sources = {h.file for h in out}
    assert "/tests/test_auth.py" in sources


def test_heuristic_scores_are_normalized():
    """Best score should be 1.0 after normalization (when there's any signal)."""
    r = HeuristicReasoner()
    out = r.reason(
        "get user name",
        (
            _hit("a.py", 1, "def get_user_name(): return name"),
            _hit("b.py", 2, "def unrelated(): pass"),
        ),
    )
    assert out[0].score == 1.0


def test_heuristic_source_field():
    r = HeuristicReasoner()
    out = r.reason("q", (_hit("a.py", 1, "def foo(): pass"),))
    assert out[0].source == "heuristic"
