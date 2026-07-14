"""Heuristic ranker — keyword-overlap + structural signals.

No LLM required. Scores candidates by:
  1. Jaccard token overlap between question and snippet (weight 0.5)
  2. Symbol match boost: question mentions a symbol that appears in the snippet (0.3)
  3. Same-directory boost when selection file and candidate share a parent dir (0.15)
  4. Test-file penalty for non-test queries (0.1)

Scores are normalized to 0–1. This is the v1 "works offline, always available"
fallback that makes results useful even without Ollama or Freebuff wired up.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import GrepHit, RankedHit

_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _same_dir(file_a: str | None, file_b: str) -> bool:
    if not file_a:
        return False
    return Path(file_a).parent == Path(file_b).parent


def _is_test_file(path: str) -> bool:
    """True if the filename looks like a test file (test_*.py, *_test.py, tests/*, etc.)."""
    name = Path(path).name.lower()
    # Only match word-boundary "test" — avoids false positives like "latest.py", "testing.py".
    if name.startswith("test_") or name.startswith("test.") or name == "test.py":
        return True
    if "_test." in name or name.endswith("_test.py"):
        return True
    return False


class HeuristicReasoner:
    """Deterministic TF-IDF-ish ranker. Compliant with the Reasoner Protocol."""

    def __init__(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "heuristic"

    def reason(
        self,
        question: str,
        candidates: tuple[GrepHit, ...],
        *,
        selection_file: str | None = None,
        selection_text: str | None = None,
    ) -> tuple[RankedHit, ...]:
        if not candidates:
            return ()

        q_tokens = _tokenize(question)
        sel_tokens = _tokenize(selection_text) if selection_text else set()

        # Detect if the user is asking about tests.
        asks_test = bool({"test", "tests", "testing"} & q_tokens)

        raw: list[tuple[int, float]] = []
        for i, c in enumerate(candidates):
            s_tokens = _tokenize(c.snippet)

            overlap = _jaccard(q_tokens, s_tokens)

            # Symbol match: do question tokens overlap with selection tokens in this snippet?
            symbol_boost = 0.0
            if sel_tokens and (sel_tokens & s_tokens):
                symbol_boost = 0.3

            # Same-directory: selection file and candidate are siblings.
            dir_boost = 0.15 if _same_dir(selection_file, c.file) else 0.0

            # Test penalty: candidate is a test file but user isn't asking about tests.
            test_penalty = 0.0
            if _is_test_file(c.file) and not asks_test:
                test_penalty = 0.1

            score = (overlap * 0.5) + symbol_boost + dir_boost - test_penalty
            raw.append((i, max(0.0, min(1.0, score))))

        # Normalize to 0–1 range if we have any positive scores.
        raw.sort(key=lambda x: x[1], reverse=True)
        scores = [s for _, s in raw]
        if scores and max(scores) > 0:
            top = max(scores)
            scores = [s / top for s in scores]

        result: list[RankedHit] = []
        for rank, (idx, _) in enumerate(raw):
            c = candidates[idx]
            result.append(RankedHit(
                file=c.file,
                line=c.line,
                snippet=c.snippet,
                score=round(scores[rank], 3),
                source="heuristic",
            ))
        return tuple(result)
