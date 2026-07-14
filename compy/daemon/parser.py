"""Step-1 parser.

V1 ships a rule-based parser — fast, deterministic, no model required for first iteration.

Per spec §2a, this slot is replaced by a tiny local model (1–3B, quantized, MLX or Ollama)
that emits the same `ParsedQuery` JSON shape. The orchestrator depends only on the contract,
so swapping is a one-line construction change.
"""

from __future__ import annotations

import re

from .models import ParsedQuery, Selection

# Question-phrase -> intent. Evaluated in order; first match wins.
_INTENT_RULES: tuple[tuple[str, str], ...] = (
    (r"\bwhy was\b|\bwho (added|changed|wrote)\b|\bwhat commit\b|\bgit blame\b|\bgit log\b|\bcommit (message|history)\b", "history"),
    (r"\bcalls?\b|\bcallers? of\b|\bwhat (calls|invokes?)\b|\bwho calls\b", "relational"),
    (r"\bimport(s|ed)?\b|\binherits?\b|\bsubclass(es)? of\b", "relational"),
    (r"\bwhere else\b|\balso uses?\b|\bother (places|uses?)\b", "references"),
    (r"\bwhere\b.*\bdefined\b|\bdefinition of\b|\bfind def\b|\bthe def of\b", "definition"),
    (r"\bwhere is\b|\bfind\b|\bwhat (does|is|handles?)\b", "fuzzy"),
)

# Confidence boost when selection contains a recognizable symbol — the spec's grounding advantage.
_SELECTION_SYMBOL_BONUS = 0.15

# Drop common stopwords when extracting fuzzy keywords.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "is", "where", "what", "does", "did", "are", "and", "of",
    "a", "an", "to", "in", "for", "with", "by", "on", "that", "this",
    "i", "want", "look", "find", "see", "show", "use", "used", "uses",
})

_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _classify_intent(question: str, has_selection: bool) -> tuple[str, float]:
    """Return (intent, base confidence) for a question."""
    q = question.lower()
    for pattern, intent in _INTENT_RULES:
        if re.search(pattern, q):
            base = 0.85 if has_selection else 0.65
            return intent, base
    # No rule matched — fuzzy with a low base; selection still helps the boost later.
    return "fuzzy", 0.50 if has_selection else 0.40


def _extract_symbol(text: str) -> str | None:
    """Best identifier-shaped token in the selection.

    Prefers snake_case names (Python-style), falls back to the longest token. The spec's
    example is `get_ability` — a snake_case function name — so this is the right default.
    """
    tokens = _SYMBOL_RE.findall(text)
    if not tokens:
        return None
    snake = [t for t in tokens if "_" in t and t.islower()]
    if snake:
        return max(snake, key=len)
    return max(tokens, key=len)


def _extract_keywords(question: str) -> tuple[str, ...]:
    q = question.lower()
    raw = (t for t in re.findall(r"[a-z][a-z0-9_]+", q) if len(t) > 2 and t not in _STOPWORDS)
    return tuple(dict.fromkeys(raw))[:8]  # de-dupe, keep order, cap at 8


class RuleBasedParser:
    """Deterministic rule-based parser. The spec's ML-shaped-but-feasible v1 safe choice."""

    def parse(self, question: str, selection_text: str | None) -> ParsedQuery:
        has_sel = bool(selection_text)
        intent, confidence = _classify_intent(question, has_sel)
        symbol = _extract_symbol(selection_text) if has_sel else None
        keywords = _extract_keywords(question) if intent == "fuzzy" else ()
        if has_sel and symbol:
            confidence = min(1.0, confidence + _SELECTION_SYMBOL_BONUS)
        # Symbol is extracted whenever the selection provides one. The orchestrator decides
        # whether to use it (intent="references"/"definition") or ignore it (intent="fuzzy"
        # branches on keywords). Earlier we cleared symbol for fuzzy, but that forced tests
        # to fudge the question to land in a non-fuzzy intent — the contract is cleaner if
        # the parser exposes everything it extracted and lets the orchestrator route.
        return ParsedQuery(
            intent=intent,
            symbol=symbol,
            keywords=keywords,
            confidence=round(confidence, 2),
        )


# Convenience for callers that treat parsing as a function call.
def parse(question: str, selection: Selection | None) -> ParsedQuery:
    return RuleBasedParser().parse(question, selection.text if selection else None)
