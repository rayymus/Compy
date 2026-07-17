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
    # Explain: "what does this function do", "explain this code", "tell me about this method".
    # Must come BEFORE rationale so "explain this function" → explain, not rationale.
    (r"\bwhat does (this|the) (code|function|method|class) do\b|\bexplain this (function|method|code|class)\b|\btell me about this (function|method|code|class)\b|\bhow does this (code|function|method|class) work\b", "explain"),
    # Graph path: "how are X and Y connected", "path from X to Y".
    (r"\bhow (are|is) .+ and .+ (connected|related)\b|\bpath (from|between)\b", "graph_path"),
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
    # Rename: "rename X to Y" — explicit triggering, never inferred.
    # \b (not ^) allows leading whitespace, punctuation, or natural-language
    # prefixes like "please rename..." — intent must still start with "rename".
    (r"\brename\s+([\w]+)\s+to\s+([\w]+)", "rename"),
    # Format / refactor: "format this file", "format the code", "/undo", "/confirm".
    (r"\bformat (this|the) (file|code|selection)\b|^/(?:undo|confirm)\b", "format"),
    # Fuzzy: catch-all for natural-language search.
    (r"\bwhere is\b|\bfind\b|\bwhat (does|is|handles?)\b", "fuzzy"),
)

# Confidence boost when selection contains a recognizable symbol — the spec's grounding advantage.
_SELECTION_SYMBOL_BONUS = 0.15

# Drop common stopwords when extracting fuzzy keywords.
_STOPWORDS: frozenset[str] = frozenset({
    # Natural-language stopwords.
    "the", "is", "where", "what", "does", "did", "are", "and", "of",
    "a", "an", "to", "in", "for", "with", "by", "on", "that", "this",
    "i", "want", "look", "find", "see", "show",
    # NOTE: "use", "used", "uses" are deliberately NOT stopwords — they're key
    # semantic signals for reference/usage queries ("where is THIS used?").
    # Programming-language keywords — searching for these floods results.
    # NOTE: "none", "true", "false" are NOT here — they're Python constants that
    # users may legitimately search for ("where is None returned").
    "def", "self", "return", "import", "class", "pass", "from", "not", "or", "if", "else", "elif", "try", "except",
    "raise", "while", "async", "await", "yield", "lambda", "global",
    "nonlocal", "as", "type", "var", "let", "const", "function", "new",
})

_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{2,}")

# Split CamelCase / PascalCase identifiers into constituent words.
#   "handleRequest" → ["handle", "Request"]
#   "HTTPServer"     → ["HTTP", "Server"]
#   "MyAPIKey"       → ["My", "API", "Key"]
# Run BEFORE lowercasing — after .lower() the boundary is destroyed and
# "handlerequest" is a single token that never matches in grep.
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

# The regex alternatives use positional capture groups.  Group pairs by format:
#   (1,2): Python — File "path", line N
#   (3,4): Node.js — at file(:| )line
#   (5,6): Go/Node/TS — file.ext:line[:col]
#   (7,8): Rust — --> file:line
# Don't reorder alternatives without updating _extract_trace_frames group indices.
_TRACE_FRAME_RE = re.compile(
    r'(?:File\s+"([^"]+)"[,:]\s*(?:line\s+)?(\d+)'  # Python
    r'|at\s+(\S+)[(:](\d+)'                             # Node.js: at file:line
    r'|(\S+\.\w+):(\d+)(?::\d+)?'                      # Go/Node: file.go:line[:col]
    r'|-->\s+(\S+):(\d+)'                                # Rust: --> file:line
    r')',
    re.IGNORECASE | re.MULTILINE,
)
_TRACE_HEADER_RE = re.compile(
    r'(?:^\s*Traceback\s*\(|^\s*panic:|^\s*Error\s*:|^\s*thread\s+)',
    re.IGNORECASE,
)


def _is_trace_input(text: str) -> bool:
    """Detect stack-trace-shaped input for zero-LLM routing."""
    return bool(_TRACE_HEADER_RE.search(text)) or bool(_TRACE_FRAME_RE.search(text))


def _extract_trace_frames(text: str) -> tuple[tuple[str, int], ...]:
    """Extract (file, line) pairs from a stack trace.

    Handles Python (File "...", line N), Node.js (at file:line),
    Go/Node/TypeScript (file.ext:line:col), and Rust (--> file:line).
    """
    frames: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for m in _TRACE_FRAME_RE.finditer(text):
        # Groups vary by which alternative matched.  Collect all (file, line) pairs.
        pairs = [
            (m.group(1), m.group(2)),   # Python: File "...", line N
            (m.group(3), m.group(4)),   # Node.js: at file:line
            (m.group(5), m.group(6)),   # Go/Node: file.ext:line[:col]
            (m.group(7), m.group(8)),   # Rust: --> file:line
        ]
        for file, line_str in pairs:
            if file and line_str and _looks_like_source_file(file):
                try:
                    key = (file, int(line_str))
                    if key not in seen:
                        frames.append(key)
                        seen.add(key)
                except ValueError:
                    pass
    return tuple(frames)


def _looks_like_source_file(path: str) -> bool:
    """Quick check: does this look like a source file path (not a URL or module path)?"""
    # Reject URLs, bare module paths (no dots), and common non-file noise.
    if path.startswith(("http://", "https://", "node:", "<")):
        return False
    # Must have a file extension or look like a real path.
    return "." in path or "/" in path


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


# Whitelist of common short tokens that are meaningful in codebases.
# Dropping ≤2-char tokens destroys searches for DB, UI, S3, id, io, go, etc.
_SHORT_TOKEN_WHITELIST: frozenset[str] = frozenset({
    "db", "ui", "io", "go", "id", "os", "s3", "ec2", "r2", "d2",
    "v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8", "v9",
    "ai", "ml", "ci", "cd", "dx", "ux", "ok", "ip", "fs", "ts",
    "js", "py", "rs", "go", "rb", "c", "r", "m",
})

# Suffix-stemming pairs: strip common suffixes so "authenticated" matches
# "authenticate" and "authenticator".  Applied to extracted keywords only.
#
# Conservative: only strip when the resulting stem is ≥4 chars AND the stem
# doesn't end in a fragment that looks broken (like "pars" from "parsing").
_STEM_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ing", ""), ("ed", ""), ("s", ""), ("es", ""), ("ly", ""),
    ("ment", ""), ("tion", ""), ("able", ""), ("ible", ""),
)

# Known stem corrections: when stripping a suffix produces a broken fragment,
# restore the correct root.  (e.g. "parsing" → "pars" → "parse").
_STEM_CORRECTIONS: dict[str, str] = {
    "pars": "parse", "parsabl": "parsable", "handl": "handle",
    "compil": "compile", "generat": "generate", "authentic": "authenticate",
    "validat": "validate", "initializ": "initialize", "optimiz": "optimize",
    "serializ": "serialize", "normaliz": "normalize",
}


def _stem_word(word: str) -> str:
    """Apply basic suffix stripping to reduce a word to its root form.

    Only strips suffixes when the stem is at least 4 characters and doesn't
    produce a broken fragment.  Corrects common suffix-stripping artifacts
    (e.g. "parsing" → "pars" → "parse").  Falls back to the original word
    when stemming would produce an unusable token.
    """
    for suffix, replacement in _STEM_SUFFIXES:
        if word.endswith(suffix):
            stem = word[: -len(suffix)] + replacement
            if len(stem) >= 4:
                # Apply known corrections for broken stems.
                return _STEM_CORRECTIONS.get(stem, stem)
    return word


# Exported so orchestrator can generate "did you mean X?" suggestions on no-match.
SYNONYM_MAP: dict[str, tuple[str, ...]] = {
    "bug": ("error", "exception", "crash", "fix"),
    "error": ("bug", "exception", "crash", "failure"),
    "auth": ("login", "authentication", "signin", "session"),
    "api": ("endpoint", "route", "handler", "controller"),
    "db": ("database", "sql", "query", "table"),
    "test": ("spec", "testing", "unittest"),
    "config": ("settings", "configuration", "env"),
    "log": ("logging", "logger", "debug", "trace"),
    "perf": ("performance", "speed", "optimize", "fast", "slow"),
    "ui": ("frontend", "view", "component", "render"),
}


def _expand_query_synonyms(keywords: tuple[str, ...]) -> tuple[str, ...]:
    """Expand keyword set with known synonyms for offline query expansion.

    Maps common domain equivalents so "bug" also matches "error", "exception",
    "crash", etc.  Only adds synonyms, never removes originals.
    """
    expanded: list[str] = list(keywords)
    for kw in keywords:
        for syn in SYNONYM_MAP.get(kw, ()):
            if syn not in expanded:
                expanded.append(syn)
    return tuple(expanded)[:12]  # Cap to prevent explosion.


def _extract_keywords(question: str) -> tuple[str, ...]:
    # Split CamelCase BEFORE lowercasing so "handleRequest" becomes
    # "handle" + "request" — two greppable keywords instead of the single
    # useless token "handlerequest" that never matches anything in the repo.
    words: list[str] = []
    for raw_word in question.split():
        if len(raw_word) <= 2 and raw_word.lower() not in _SHORT_TOKEN_WHITELIST:
            continue
        camel_parts = _split_camel_case(raw_word)
        if len(camel_parts) > 1:
            words.extend(p.lower() for p in camel_parts)
        else:
            words.append(raw_word.lower())
    # Filter stopwords.  Keep whitelisted short tokens even if ≤2 chars.
    words = [
        w for w in words
        if (len(w) > 2 or w in _SHORT_TOKEN_WHITELIST) and w not in _STOPWORDS
    ]
    # Fallback: when stopword/length filtering strips EVERYTHING (e.g. "show me the
    # graph" → all words are stopwords or too short), bypass the filters and use the
    # raw tokens so the grepper has SOMETHING to search for.
    # Only keep non-stopword tokens ≥3 chars — falling back to "where"/"is"/"the"
    # would flood results with noise.
    if not words:
        words = [w.lower() for w in question.split() if len(w) >= 3 and w not in _STOPWORDS]
    # Apply stemming to reduce morphological variants.
    words = [_stem_word(w) for w in words]
    # Deduplicate after stemming ("authenticated" + "authenticates" → "authenticate").
    words = list(dict.fromkeys(words))
    phrases: list[str] = []
    for i in range(len(words) - 1):
        phrases.append(f"{words[i]} {words[i+1]}")
    if len(words) >= 3:
        for i in range(len(words) - 2):
            phrases.append(f"{words[i]} {words[i+1]} {words[i+2]}")
    # Combine single words + phrases, dedupe, cap at 8.
    raw = phrases + [t for t in words if len(t) > 2 or t in _SHORT_TOKEN_WHITELIST]
    keywords = tuple(dict.fromkeys(raw))[:8]
    # Expand with known synonyms for broader recall.
    return _expand_query_synonyms(keywords)


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
        # Gap 1 (Session 20): Deictic reference promotion.
        # When the user selects a symbol and asks "where is this used",
        # the selection IS the thing they're asking about — promote to
        # references so the orchestrator searches for the symbol directly
        # instead of falling through to fuzzy keyword grep.
        # Only fires when there IS a selection ("this" without a selection
        # has nothing to point at).
        if has_sel and symbol and intent == "fuzzy":
            q_lower = question.lower()
            if re.search(r"\b(this|that|it)\b", q_lower) and re.search(r"\b(used|called|referenced|invoked)\b", q_lower):
                intent = "references"
                confidence = max(confidence, 0.85)
        # explain requires a selection — without one, it's just a fuzzy question.
        if intent == "explain" and not has_sel:
            intent = "fuzzy"
            confidence = 0.50
        # Pre-extract keywords once — graph_path needs them for symbol extraction.
        _pre_keywords = _extract_keywords(question) if intent in ("fuzzy", "convention", "dedup", "overview", "graph_path", "explain") else ()
        # graph_path: extract two symbols from keywords (longest non-stopword tokens).
        if intent == "graph_path":
            syms = [kw for kw in _pre_keywords if len(kw) >= 2 and " " not in kw][:2]
            if len(syms) >= 2:
                symbol = f"{syms[0]}::{syms[1]}"
                confidence = max(confidence, 0.75)
            else:
                intent = "fuzzy"
                confidence = 0.45
        # rename: extract "old_name::new_name" from the regex capture groups.
        if intent == "rename":
            m = re.search(r"\brename\s+([\w]+)\s+to\s+([\w]+)", question, re.IGNORECASE)
            if m:
                symbol = f"{m.group(1)}::{m.group(2)}"
                confidence = 0.95
            else:
                intent = "fuzzy"
                confidence = 0.40
        keywords = _pre_keywords
        # Gap 2 (Session 20): Inject the selection symbol into fuzzy keywords.
        # The user selected a piece of code — the symbol from that code should
        # be the first thing we search for. Without this, the fuzzy path
        # searches only question keywords and ignores the selected symbol entirely.
        if intent == "fuzzy" and symbol:
            kw_list = list(keywords)
            if symbol.lower() not in kw_list and symbol not in kw_list:
                kw_list.insert(0, symbol)
            keywords = tuple(kw_list)
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
