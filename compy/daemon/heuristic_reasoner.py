"""Heuristic ranker — keyword-overlap + structural signals.

No LLM required. Scores candidates by:
  1. Jaccard token overlap between question and snippet (weight 0.25)
  2. Filename relevance: query tokens matching filename components (0.15)
  3. Exact symbol match: query contains an identifier that appears in snippet (0.25)
  4. Same-directory boost when selection file and candidate share a parent dir (0.15)
  5. N-gram adjacency: query bigrams appearing in snippet (0.10)
  6. Token frequency weighting: rare tokens weighted higher via local IDF (0.10)
  7. Test-file penalty for non-test queries (-0.10)

Scores are normalized to 0–1. This is the v1 "works offline, always available"
fallback that makes results useful even without Ollama or Freebuff wired up.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import GrepHit, RankedHit

_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")
_EXACT_WORD_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b")

# Common tokens that appear everywhere — penalized in frequency weighting.
_COMMON_CODE_TOKENS: frozenset[str] = frozenset({
    "def", "self", "return", "import", "class", "pass", "none", "true",
    "false", "from", "this", "that", "with", "for", "not", "and", "or",
    "if", "else", "elif", "try", "except", "raise", "while", "in", "is",
    "async", "await", "yield", "lambda", "global", "nonlocal", "as",
})


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
    if name.startswith("test_") or name.startswith("test.") or name == "test.py":
        return True
    if "_test." in name or name.endswith("_test.py"):
        return True
    return False


def _filename_tokens(filepath: str) -> set[str]:
    """Extract meaningful tokens from a filename (e.g. 'auth_handler.py' → {'auth', 'handler', 'py'})."""
    name = Path(filepath).stem.lower()
    # Split on underscores, hyphens, dots, camelCase boundaries.
    parts = re.split(r"[_.\-]", name)
    result: set[str] = set()
    for p in parts:
        # Split camelCase: "authHandler" → ["auth", "Handler"]
        camel = re.sub(r"([a-z])([A-Z])", r"\1_\2", p).split("_")
        for c in camel:
            c_lower = c.lower()
            if len(c_lower) >= 2 and c_lower not in _COMMON_CODE_TOKENS:
                result.add(c_lower)
    # Also add the whole stem as one token.
    if len(name) >= 2:
        result.add(name)
    return result


def _exact_word_matches(query: str, snippet: str) -> float:
    """Count how many query words appear as exact identifiers in the snippet."""
    q_words = {w.lower() for w in _EXACT_WORD_RE.findall(query) if len(w) > 2}
    if not q_words:
        return 0.0
    s_words = {w.lower() for w in _EXACT_WORD_RE.findall(snippet)}
    matches = len(q_words & s_words)
    return min(1.0, matches / max(1, len(q_words)))


def _token_rarity_weight(tokens: set[str], all_snippets: list[str]) -> dict[str, float]:
    """Boost rare tokens, penalize common ones. Returns weight per token (0–1).

    Uses a corpus-relative IDF: rarer tokens (appearing in fewer snippets) get higher
    weight.  Common code tokens are zeroed entirely.  The all_snippets pool is the
    grep candidate set, not the whole repo — this is intentionally local, not global
    IDF, because the grep candidates are already filtered to the query's domain.
    """
    if not tokens or not all_snippets:
        return {}
    weights: dict[str, float] = {}
    for tok in tokens:
        if tok in _COMMON_CODE_TOKENS:
            weights[tok] = 0.0
            continue
        # Count how many snippets contain this token.
        appearing = sum(1 for s in all_snippets if tok in _tokenize(s))
        # IDF-like: rarer tokens get higher weight.  Avoid divide-by-zero.
        idf = 1.0 / (1.0 + appearing)
        weights[tok] = round(idf, 3)
    return weights


def _ngram_overlap(query: str, snippet: str, n: int = 2) -> float:
    """Score snippet by how many query bigrams appear adjacent in the snippet.

    Jaccard ignores word order — "user delete" and "delete user" score identically.
    Bigram overlap catches adjacency: when the query's consecutive word pairs appear
    in the snippet, the snippet is more likely relevant.
    """
    q_words = [w.lower() for w in _EXACT_WORD_RE.findall(query) if len(w) > 1]
    s_words = [w.lower() for w in _EXACT_WORD_RE.findall(snippet)]
    if len(q_words) < n or len(s_words) < n:
        return 0.0
    q_ngrams = {tuple(q_words[i:i + n]) for i in range(len(q_words) - n + 1)}
    s_ngrams = {tuple(s_words[i:i + n]) for i in range(len(s_words) - n + 1)}
    if not q_ngrams:
        return 0.0
    return len(q_ngrams & s_ngrams) / len(q_ngrams)


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

        # Layer 2: fold structural context (callers, verification) into the
        # scoring text so token-overlap naturally benefits from graph signal.
        scoring_snippets = [
            f"{c.snippet} {c.context or ''}" for c in candidates
        ]

        # Precompute token rarity across all candidate snippets.
        rarity_weights = _token_rarity_weight(q_tokens, scoring_snippets)

        raw: list[tuple[int, float]] = []
        for i, c in enumerate(candidates):
            # Layer 2: enrich snippet with structural context so token-overlap
            # scoring naturally benefits from graph relationships (callers etc.).
            enriched_snippet = f"{c.snippet} {c.context or ''}"
            s_tokens = _tokenize(enriched_snippet)

            # 1. Jaccard overlap (0.25 weight).
            jaccard = _jaccard(q_tokens, s_tokens)

            # 2. Filename relevance (0.15 weight).
            fname_tokens = _filename_tokens(c.file)
            fname_overlap = _jaccard(q_tokens, fname_tokens)

            # 3. Exact symbol match (0.25 weight) — use enriched text.
            exact = _exact_word_matches(question, enriched_snippet)

            # 4. Same-directory boost (0.15 weight).
            dir_boost = 0.15 if _same_dir(selection_file, c.file) else 0.0

            # 5. N-gram adjacency: query bigrams appearing in snippet (0.10 weight).
            ngram = _ngram_overlap(question, enriched_snippet)

            # 6. Token rarity: weighted Jaccard using IDF-like weights (0.10 weight).
            rare_overlap = 0.0
            common = q_tokens & s_tokens
            if common:
                rare_overlap = sum(rarity_weights.get(t, 0.0) for t in common) / max(1, len(q_tokens))

            # 6. Test penalty.
            test_penalty = 0.1 if (_is_test_file(c.file) and not asks_test) else 0.0

            score = (
                (jaccard * 0.25)
                + (fname_overlap * 0.15)
                + (exact * 0.25)
                + dir_boost
                + (ngram * 0.10)
                + (rare_overlap * 0.10)
                - test_penalty
            )
            raw.append((i, max(0.0, min(1.0, score))))

        # Sort by score descending.
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
                structural_context=c.context,
            ))
        return tuple(result)
