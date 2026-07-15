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
    # Trace input: stack traces, error dumps with file:line frames.
    # Note: _classify_intent lowercases first, so patterns use lowercase.
    # Non-anchored — trace lines may appear mid-string after a header.
    (r"file\s+\"[^\"]+\"\s*[,:]\s*(?:line\s+)?\d+", "trace"),
    (r"\bat\s+\S+[(:]\d+", "trace"),
    (r"traceback\s*\(", "trace"),
    # History / rationale: why something exists, who changed it.
    (r"\bwhy was\b|\bwho (added|changed|wrote)\b|\bwhat commit\b|\bgit blame\b|\bgit log\b|\bcommit (message|history)\b", "history"),
    (r"\bwhy (does|is)\b.*\b(exist|here|this|that)\b|\breason for\b|\bexplain this\b|\bwhy would\b", "rationale"),
    # Relational: call graph, imports, inheritance.
    (r"\bcalls?\b|\bcallers? of\b|\bwhat (calls|invokes?)\b|\bwho calls\b", "relational"),
    (r"\bimport(s|ed)?\b|\binherits?\b|\bsubclass(es)? of\b", "relational"),
    # Blast radius: impact analysis, dependency checking.
    (r"\bwhat (breaks|depends|relies)\b|\bblast radius\b|\bimpact of\b|\bwhat would break\b", "blast_radius"),
    # References / definition.
    (r"\bwhere else\b|\balso uses?\b|\bother (places|uses?)\b", "references"),
    (r"\bwhere\b.*\bdefined\b|\bdefinition of\b|\bfind def\b|\bthe def of\b", "definition"),
    # Convention / precedent: how do we normally do X.
    (r"\bhow (do|should) (we|i|you)\b|\bwhat('s| is) the pattern\b|\bshow (me |)(examples?|usages?)\b|\bconvention for\b|\bhow (is|are)\b.*\b(typically|usually|normally)\b", "convention"),
    # Dedup check: does this already exist? (same mechanism as convention, different trigger).
    (r"\bdoes this (already )?exist\b|\bis there (already )\b|\bduplicate of\b|\banyone (already )?(done|wrote|built)\b|\bhas this (been |)(done|written|built)\b|\bis (this|that|it) (a |)(duplicate|similar)\b", "dedup"),
    # Dead code: unused symbols, zero references.
    (r"\bwhat('s| is) unused\b|\bdead code\b|\bwhat isn('t|ot) (being |)used\b|\bfind unused\b|\bunreferenced\b", "dead_code"),
    # Overview / catch-up Q&A: broad repo questions about architecture, flow, structure.
    (r"\bhow does\b|\bgive me an overview\b|\bexplain the (codebase|repo|architecture|structure)\b|\bhow (is|are)\b.*\b(organized|structured|laid out)\b|\bwhat (is|are) the (main|key) (modules?|components?|parts?)\b", "overview"),
    # Fuzzy: catch-all for natural-language search.
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

# Split CamelCase / PascalCase identifiers into constituent words.
#   "handleRequest" → ["handle", "Request"]
#   "HTTPServer"     → ["HTTP", "Server"]
#   "MyAPIKey"       → ["My", "API", "Key"]
# Run BEFORE lowercasing — after .lower() the boundary is destroyed and
# "handlerequest" is a single token that never matches in grep.
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# Stack trace frame detection — zero-LLM routing for pasted errors.
_TRACE_FRAME_RE = re.compile(
    r'^\s*(?:File\s+"([^"]+)"|at\s+(\S+))[,:]?\s*(?:line\s+)?(\d+)',
    re.IGNORECASE | re.MULTILINE,
)
_TRACE_HEADER_RE = re.compile(r'^\s*Traceback\s*\(', re.IGNORECASE)


def _is_trace_input(text: str) -> bool:
    """Detect stack-trace-shaped input for zero-LLM routing."""
    return bool(_TRACE_HEADER_RE.search(text)) or bool(_TRACE_FRAME_RE.search(text))


def _extract_trace_frames(text: str) -> tuple[tuple[str, int], ...]:
    """Extract (file, line) pairs from a stack trace."""
    frames: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for m in _TRACE_FRAME_RE.finditer(text):
        file = m.group(1) or m.group(2) or ""
        line_str = m.group(3)
        if file and line_str:
            key = (file, int(line_str))
            if key not in seen:
                frames.append(key)
                seen.add(key)
    return tuple(frames)


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


def _split_camel_case(word: str) -> list[str]:
    """Split a CamelCase/PascalCase word into its constituent parts."""
    parts = _CAMEL_SPLIT_RE.split(word)
    return [p for p in parts if len(p) > 1]


def _extract_keywords(question: str) -> tuple[str, ...]:
    # Split CamelCase BEFORE lowercasing so "handleRequest" becomes
    # "handle" + "request" — two greppable keywords instead of the single
    # useless token "handlerequest" that never matches anything in the repo.
    words: list[str] = []
    for raw_word in question.split():
        if len(raw_word) <= 2:
            continue
        camel_parts = _split_camel_case(raw_word)
        if len(camel_parts) > 1:
            words.extend(p.lower() for p in camel_parts)
        else:
            words.append(raw_word.lower())
    words = [w for w in words if len(w) > 2 and w not in _STOPWORDS]
    phrases: list[str] = []
    for i in range(len(words) - 1):
        phrases.append(f"{words[i]} {words[i+1]}")
    if len(words) >= 3:
        for i in range(len(words) - 2):
            phrases.append(f"{words[i]} {words[i+1]} {words[i+2]}")
    # Combine single words + phrases, dedupe, cap at 8.
    raw = phrases + [t for t in words if len(t) > 2]
    return tuple(dict.fromkeys(raw))[:8]


class RuleBasedParser:
    """Deterministic rule-based parser. The spec's ML-shaped-but-feasible v1 safe choice.

    Each parse decision is logged to /tmp/compy-parse-decisions.log — this is the dataset
    that P5's MLX/Ollama parser replacement will train on. Claude's deep-reasoning flagged
    that 102 passing tests prove stability, not real-world accuracy on actual user phrasing.
    Logging decisions now builds the training corpus before the replacement exists.
    """

    def parse(self, question: str, selection_text: str | None) -> ParsedQuery:
        has_sel = bool(selection_text)
        intent, confidence = _classify_intent(question, has_sel)
        symbol = _extract_symbol(selection_text) if has_sel else None
        keywords = _extract_keywords(question) if intent in ("fuzzy", "convention", "dedup", "overview") else ()
        if has_sel and symbol:
            confidence = min(1.0, confidence + _SELECTION_SYMBOL_BONUS)

        result = ParsedQuery(
            intent=intent,
            symbol=symbol,
            keywords=keywords,
            confidence=round(confidence, 2),
        )
        _log_parse_decision(question, has_sel, result)
        return result


def _log_parse_decision(question: str, has_selection: bool, result: ParsedQuery) -> None:
    """Log parse decisions for P5 training data.

    Each entry is a JSON line written to /tmp/compy-parse-decisions.log.
    Non-essential — failures are swallowed silently so the pipeline never
    breaks on logging.
    """
    try:
        import json
        from pathlib import Path
        entry = json.dumps({
            "question": question[:200],
            "has_selection": has_selection,
            "intent": result.intent,
            "symbol": result.symbol,
            "keywords": list(result.keywords),
            "confidence": result.confidence,
        })
        log_path = Path("/tmp/compy-parse-decisions.log")
        with open(log_path, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass  # Never let logging break the pipeline.


# Convenience for callers that treat parsing as a function call.
def parse(question: str, selection: Selection | None) -> ParsedQuery:
    return RuleBasedParser().parse(question, selection.text if selection else None)
