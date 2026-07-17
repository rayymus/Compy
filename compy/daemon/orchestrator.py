"""Pipeline orchestrator: parse -> grep -> reason, with cascading fallback.

Per spec §3a, the result panel must NEVER present a hard error — every failure degrades
into either "grep-only hits" or an empty "no results" hint state. This module reads
exactly that way: no raised exceptions escape.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace

from .interfaces import Grapher, Grepper, Historian, Parser, Reasoner, ReasonerUnavailable
from .models import (
    GrepHit,
    ParsedQuery,
    QueryRequest,
    QueryResult,
    RankedHit,
    Selection,
)
from .parser import SYNONYM_MAP, _extract_trace_frames, _is_trace_input

# Regex to pull a Python symbol name from a snippet (def/class).
_SYMBOL_FROM_SNIPPET = re.compile(
    r'\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)'
)

# Smart grep patterns — generated per-intent, not hard-coded per-word.
# The overlay passes "find definition of X" → intent="definition", symbol="X"
# → we gen patterns like r"def\s+X\b" so grep finds actual definitions,
# not random usages of the word X.
_INTENT_PATTERNS: dict[str, list[str]] = {
    "definition": [
        r"def\s+{symbol}\b",        # Python: def, async def
        r"class\s+{symbol}\b",      # Python: class
        r"\bfunc\s+{symbol}\b",     # Go/Rust: func / function
        r"{symbol}\s*=\s*lambda\b", # Python: lambda assignment
        r"const\s+{symbol}\b",      # JS/TS: const
    ],
    "references": [
        r"{symbol}\b",  # bare symbol catches call sites, imports, usage
    ],
}

# TBD-before-impl tunable per spec §2a. 0.6 is the documented starting guess — measure on
# real queries, tighten later.
FUZZY_THRESHOLD = 0.6

# Direct-hit shortcut: short, exact-match lists don't need LLM ranking (spec §2 "Why this
# matters"). 3 is a guess; tune by measuring whether users tolerate ranked vs. flat for
# 4-hit cases.
DIRECT_HIT_MAX = 3

# Cap on fuzzy retries — don't bash the disk with every stopword.
MAX_FUZZY_KEYWORD_TRIES = 5


def run(
    request: QueryRequest,
    *,
    parser: Parser,
    grepper: Grepper,
    reasoners: tuple[Reasoner, ...],
    grapher: Grapher | None = None,
    historian: Historian | None = None,
    on_candidates: Callable[[tuple[GrepHit, ...]], None] | None = None,
) -> QueryResult:
    if not request.question.strip():
        return QueryResult(intent="empty", hits=(), reason="empty question")

    parsed = parser.parse(request.question, _selection_text(request.selection))
    # workspace_root can be None when the Swift overlay sends a Selection without it
    # (e.g. clipboard-swap fallback with no extension JSON). Never pass None to rg.
    workspace = (
        request.selection.workspace_root or "."
        if request.selection
        else "."
    )

    parsed, hits, degraded, reason = _evaluate(
        parsed=parsed,
        parser=parser,
        grepper=grepper,
        reasoners=reasoners,
        question=request.question,
        workspace=workspace,
        selection=request.selection,
        grapher=grapher,
        historian=historian,
        on_candidates=on_candidates,
        session_context=request.session_context,
    )
    # Annotate results with structural context from Graphify (callers/importers).
    # Since Layer 2 enrichment already computed context per GrepHit, and reasoners
    # propagate it to RankedHit.structural_context, no duplicate Graphify query needed.
    suggestions = _generate_suggestions(parsed, request.selection) if not hits else None
    return QueryResult(intent=parsed.intent, hits=hits, degraded=degraded, reason=reason, suggestions=suggestions)


def _evaluate(
    *,
    parsed: ParsedQuery,
    parser: Parser,
    grepper: Grepper,
    reasoners: tuple[Reasoner, ...],
    question: str,
    workspace: str,
    selection: Selection | None = None,
    grapher: Grapher | None = None,
    historian: Historian | None = None,
    on_candidates: Callable[[tuple[GrepHit, ...]], None] | None = None,
    session_context: tuple[str, ...] | None = None,
) -> tuple[ParsedQuery, tuple[RankedHit, ...], bool, str | None]:
    sel_file = selection.file if selection else None
    sel_text = selection.text if selection else None

    # --- Trace path: stack traces get zero-LLM structural search ---
    if parsed.intent == "trace":
        # Check the question text first — that's where the user pasted the trace.
        # Fall back to selection text only if the question is very short.
        trace_text = question if len(question) > 40 else (sel_text or question)
        if _is_trace_input(trace_text):
            frames = _extract_trace_frames(trace_text)
            if frames:
                trace_hits: list[RankedHit] = []
                for file, line in frames:
                    try:
                        snippet = _read_line(file, line, workspace)
                    except (OSError, ValueError):
                        snippet = f"{file}:{line}"
                    trace_hits.append(RankedHit(
                        file=file, line=line, snippet=snippet,
                        score=1.0, source="trace",
                    ))
                if trace_hits:
                    return parsed, tuple(trace_hits), False, None
        # No frames extracted — fall through to fuzzy.
        parsed = replace(parsed, intent="fuzzy")

    # --- Graphify path: relational + blast radius queries ---
    if parsed.intent in ("relational", "blast_radius") and grapher is not None:
        try:
            grapher.load(workspace)
        except ReasonerUnavailable:
            pass  # Fall through — graph unavailable, try fuzzy grep.
        else:
            # "callers of" / "who calls" → callers; "what does X call" → calls
            if parsed.intent == "blast_radius":
                sub_intent = "blast_radius"
            elif ("callers of" in question.lower() or "who calls" in question.lower()):
                sub_intent = "callers"
            elif "calls" in question.lower():
                sub_intent = "calls"
            else:
                sub_intent = "calls"
            if "import" in question.lower():
                sub_intent = "imports"
            elif "inherit" in question.lower() or "subclass" in question.lower():
                sub_intent = "subclasses"
            symbol = parsed.symbol or _first_keyword(parsed.keywords)
            if symbol:
                candidates = grapher.query(symbol, intent=sub_intent)
                if candidates:
                    return _rank_or_degrade(
                        parsed=parsed, reasoners=reasoners,
                        question=question, candidates=candidates,
                        selection_file=sel_file, selection_text=sel_text,
                        on_candidates=on_candidates,
                        grapher=grapher, historian=historian,
                        workspace=workspace, session_context=session_context,
                    )
        # Graph returned empty — fall through to fuzzy grep.
        parsed = replace(parsed, intent="fuzzy")

    # --- Git history path: history + rationale queries ---
    if parsed.intent in ("history", "rationale") and historian is not None:
        candidates = historian.query_history(question, workspace)
        if not candidates and selection:
            candidates = historian.query_file_history(sel_file or "", workspace)
        if candidates:
            return _rank_or_degrade(
                parsed=parsed, reasoners=reasoners,
                question=question, candidates=candidates,
                selection_file=sel_file, selection_text=sel_text,
                on_candidates=on_candidates,
                grapher=grapher, historian=historian,
                workspace=workspace, session_context=session_context,
            )
        # No git history found — fall through to fuzzy grep.
        parsed = replace(parsed, intent="fuzzy")

    # Confidence-too-low drops into fuzzy (spec §2a).
    if parsed.confidence < FUZZY_THRESHOLD:
        parsed = replace(parsed, intent="fuzzy")

    # Convention / dedup path: route to fuzzy semantic search.
    if parsed.intent in ("convention", "dedup"):
        parsed = replace(parsed, intent="fuzzy")

    # --- Overview / catch-up Q&A path: structural digest via Graphify ---
    if parsed.intent == "overview":
        if grapher is not None:
            try:
                grapher.load(workspace)
            except ReasonerUnavailable:
                pass  # Fall through to fuzzy.
            else:
                candidates = grapher.query_overview()
                if candidates:
                    # Overview hits are descriptive — no reasoner ranking needed.
                    # Promote directly with the graph source label.
                    hits = tuple(
                        RankedHit(
                            file=h.file, line=h.line, snippet=h.snippet,
                            score=1.0, source="graph",
                        )
                        for h in candidates
                    )
                    return parsed, hits, False, None
            # Graph unavailable or returned empty — fall through to fuzzy.
        parsed = replace(parsed, intent="fuzzy")

    # --- Dead-code path: find unused symbols via Graphify ---
    if parsed.intent == "dead_code" and grapher is not None:
        try:
            grapher.load(workspace)
        except ReasonerUnavailable:
            parsed = replace(parsed, intent="fuzzy")
        else:
            candidates = grapher.query_dead_code()
            if candidates:
                return _rank_or_degrade(
                    parsed=parsed, reasoners=reasoners,
                    question=question, candidates=candidates,
                    selection_file=sel_file, selection_text=sel_text,
                    on_candidates=on_candidates,
                    grapher=grapher, historian=historian,
                    workspace=workspace, session_context=session_context,
                )
        parsed = replace(parsed, intent="fuzzy")
    # If grapher is None, fall through to fuzzy.
    if parsed.intent == "dead_code":
        parsed = replace(parsed, intent="fuzzy")

    # Structured path: parse a symbol, search with intent-aware patterns.
    if parsed.intent in ("references", "definition") and parsed.symbol:
        patterns = _gen_grep_patterns(parsed.symbol, parsed.intent)
        # Try patterns in order — first match wins.  The definition-specific
        # patterns (def\s+X, class\s+X) come first; bare symbol as fallback.
        hits: tuple[GrepHit, ...] = ()
        for pat in patterns:
            try:
                hits = grepper.grep(pat, workspace)
            except ReasonerUnavailable:
                continue
            if hits:
                break
        if not hits:
            # Fallback: try bare symbol (catches definitions in unexpected formats).
            try:
                hits = grepper.grep(parsed.symbol, workspace)
            except ReasonerUnavailable as exc:
                return parsed, (), True, f"grep failed: {exc}"

        if not hits:
            # §2a fallback: zero grep hits → fuzzy branch with keywords if any.
            parsed = replace(parsed, intent="fuzzy")
        elif len(hits) <= DIRECT_HIT_MAX:
            return parsed, _promote_grep(hits, direct=True), False, None
        else:
            return _rank_or_degrade(
                parsed=parsed, reasoners=reasoners,
                question=question, candidates=hits,
                selection_file=sel_file, selection_text=sel_text,
                on_candidates=on_candidates,
                grapher=grapher, historian=historian,
                workspace=workspace, session_context=session_context,
            )

    # Fuzzy path: try multi-keyword AND search, then individual keywords.
    if parsed.intent == "fuzzy":
        candidates: tuple[GrepHit, ...] = ()
        keywords = list(parsed.keywords)[:MAX_FUZZY_KEYWORD_TRIES]
        # Strategy 1: Multi-keyword OR search (match any keyword, broader recall).
        if len(keywords) >= 2:
            and_pattern = "|".join(keywords[:4])
            try:
                candidates = grepper.grep(and_pattern, workspace)
                # Boost hits from selection file to top.
                if sel_file:
                    candidates = _boost_selection_file(candidates, sel_file)
            except ReasonerUnavailable:
                pass
        # Strategy 2: Fall back to individual keyword search (union of results).
        # Merges hits from each keyword for broader recall.  Only single-word
        # keywords (skip phrases like "auth flow") — phrases are covered by
        # the multi-keyword OR strategy.  Capped at MAX_FUZZY_KEYWORD_TRIES.
        if not candidates and len(keywords) > 0:
            singles = [kw for kw in keywords if " " not in kw][:MAX_FUZZY_KEYWORD_TRIES]
            if singles:
                seen: set[tuple[str, int]] = set()
                merged: list[GrepHit] = []
                for kw in singles:
                    try:
                        for h in grepper.grep(kw, workspace):
                            key = (h.file, h.line)
                            if key not in seen:
                                seen.add(key)
                                merged.append(h)
                    except ReasonerUnavailable:
                        continue
                if merged:
                    if sel_file:
                        candidates = _boost_selection_file(tuple(merged), sel_file)
                    else:
                        candidates = tuple(merged)
        if not candidates:
            return parsed, (), False, "no hits"

        return _rank_or_degrade(
            parsed=parsed, reasoners=reasoners,
            question=question, candidates=candidates,
            selection_file=sel_file, selection_text=sel_text,
            on_candidates=on_candidates,
            grapher=grapher, historian=historian,
            workspace=workspace, session_context=session_context,
        )
    return parsed, (), False, "no actionable intent"


def _rank_or_degrade(
    *,
    parsed: ParsedQuery,
    reasoners: tuple[Reasoner, ...],
    question: str,
    candidates: tuple[GrepHit, ...],
    selection_file: str | None = None,
    selection_text: str | None = None,
    on_candidates: Callable[[tuple[GrepHit, ...]], None] | None = None,
    grapher: Grapher | None = None,
    historian: Historian | None = None,
    workspace: str = ".",
    session_context: tuple[str, ...] | None = None,
) -> tuple[ParsedQuery, tuple[RankedHit, ...], bool, str | None]:
    """Try the reasoner chain. Every failure falls through, all-failure degrades to grep hint."""
    # Stream intermediate candidates to the overlay BEFORE blocking on reasoner.
    if on_candidates and candidates:
        on_candidates(candidates)

    # ── Layer 2: enrich candidates with structural context before ranking ──
    # Graph relationships, verification hints, git history, and session memory
    # give even a small local model enough signal to rank well.
    enriched = _enrich_candidates_for_ranking(
        candidates, grapher=grapher, historian=historian, workspace=workspace,
        question=question,
    )

    # ── Layer 0: inject prior-turn results into the question for follow-ups ──
    # "Show me the tests for that" → the reasoner sees what "that" was.
    enriched_question = question
    if session_context:
        ctx_text = "Previous results (for context only — answer the current question):\n" + "\n".join(
            s[:120] for s in session_context[:3]
        )
        enriched_question = f"{ctx_text}\n---\nCurrent question: {question}"

    last_err: str | None = None
    for r in reasoners:
        try:
            ranked = r.reason(enriched_question, enriched,
                              selection_file=selection_file,
                              selection_text=selection_text)
        except ReasonerUnavailable as exc:
            last_err = f"{r.name}: {exc}"
            continue
        if ranked:
            return parsed, ranked, False, None
        # Returned empty — fall through to the next reasoner per spec §3a.
        last_err = f"{r.name}: returned empty"
    # Spec §3a: never hard-error; grep-only hits with a note.
    return parsed, _promote_grep(candidates, direct=False), True, f"all reasoners unavailable; {last_err or 'no reasoners configured'}"


def _enrich_candidates_for_ranking(
    candidates: tuple[GrepHit, ...],
    *,
    grapher: Grapher | None = None,
    historian: Historian | None = None,
    workspace: str = ".",
    question: str = "",
) -> tuple[GrepHit, ...]:
    """Annotate GrepHit candidates with structural context before rankers see them.

    Layer 2 from claude-response2: the reasoner (Ollama 1.5B or heuristic) gets richer
    input — graph relationships, verification hints, git history — instead of raw grep text.

    Graph lookups use fast_only=True to avoid blocking on large repos: if no cached
    graph exists, enrichment is silently skipped and the reasoner works with bare text.
    """
    if grapher is None and historian is None:
        return candidates

    # Try fast-load: only use cached graph, don't rebuild.
    graph_loaded = False
    if grapher is not None:
        try:
            grapher.load(workspace, fast_only=True)
            graph_loaded = True
        except ReasonerUnavailable:
            pass  # No cached graph — skip graph enrichment.

    enriched: list[GrepHit] = []
    for c in candidates:
        ctx_parts: list[str] = []

        # Graph relationships: callers/callees for the hit's symbol.
        if graph_loaded:
            symbol = _extract_symbol_from_snippet(c.snippet)
            if symbol:
                try:
                    callers = grapher.query(symbol, intent="callers")  # type: ignore[union-attr]
                    if callers:
                        names = _caller_names(callers, max_items=3)
                        if names:
                            ctx_parts.append(f"Called by: {names}")
                except Exception:
                    pass  # Silently skip — enrichment is best-effort.

        # Git history: blame info — only for history/rationale queries.
        is_history_q = historian is not None and _looks_like_history_query(question)
        if is_history_q:
            try:
                blame_hits = historian.query_blame(c.file, c.line, workspace)
                if blame_hits:
                    snippet = blame_hits[0].snippet[:80]
                    if snippet and "Not a git repository" not in snippet:
                        ctx_parts.append(f"git: {snippet}")
            except Exception:
                pass

        # Lightweight verification: snippet contains the expected kind of syntax.
        verified = _verify_snippet_kind(c.snippet)
        if verified:
            ctx_parts.append(verified)

        ctx = "; ".join(ctx_parts) if ctx_parts else None
        enriched.append(GrepHit(
            file=c.file, line=c.line, column=c.column,
            snippet=c.snippet, symbol=c.symbol, context=ctx,
        ))
    return tuple(enriched)


def _looks_like_history_query(question: str) -> bool:
    """Quick check: does this question ask about git history / blame / rationale?"""
    q = question.lower()
    return any(w in q for w in ("why", "who added", "who changed", "who wrote",
                                 "blame", "commit", "history", "rationale",
                                 "what changed", "when was"))


def _verify_snippet_kind(snippet: str) -> str | None:
    """Lightweight check: what kind of code construct is this snippet?

    Returns a short label like 'def' or 'class' if the snippet starts with a
    recognised definition keyword, so the reasoner knows this is a real definition
    rather than a comment or a usage in a docstring.
    """
    stripped = snippet.lstrip()
    if stripped.startswith("def ") or stripped.startswith("async def "):
        return "def"
    if stripped.startswith("class "):
        return "class"
    if stripped.startswith(("func ", "function ")):
        return "func"
    if " = lambda" in stripped:
        return "lambda"
    return None


def _promote_grep(
    hits: tuple[GrepHit, ...], *, direct: bool,
) -> tuple[RankedHit, ...]:
    """Convert raw GrepHits into displayable RankedHits with monotonic scores.

    Caller passes `direct=True` from the direct-hit shortcut path (≤DIRECT_HIT_MAX hits, no
    LLM, degraded=False) — those are surfaced at score=1.0 to convey "this is THE answer."
    From the degraded-fallback path (all reasoners failed), caller passes `direct=False` —
    hits preserve order via monotonically descending scores so the UI shows the candidates
    in some sensible order, and `degraded=True` on the result carries the warning.
    """
    return tuple(
        RankedHit(
            file=h.file, line=h.line, snippet=h.snippet,
            score=1.0 if direct else 1.0 / (i + 1),
            source="grep",
            structural_context=h.context,
        )
        for i, h in enumerate(hits)
    )


def _selection_text(sel: Selection | None) -> str | None:
    return sel.text if sel and sel.text else None


def _first_keyword(keywords: tuple[str, ...]) -> str | None:
    return keywords[0] if keywords else None


def _boost_selection_file(
    hits: tuple[GrepHit, ...], selection_file: str
) -> tuple[GrepHit, ...]:
    """Promote hits from the user's current file to the top of results."""
    if not selection_file:
        return hits
    in_file = [h for h in hits if h.file == selection_file or h.file.endswith(selection_file)]
    other = [h for h in hits if h not in in_file]
    return tuple(in_file + other)


def _extract_symbol_from_snippet(snippet: str) -> str | None:
    """Pull a function/class name from a Python snippet like 'def foo():'."""
    m = _SYMBOL_FROM_SNIPPET.search(snippet)
    return m.group(1) if m else None


def _caller_names(callers: tuple[GrepHit, ...], *, max_items: int = 2) -> str:
    """Extract short display names from Graphify GrepHits.

    Graphify stores node IDs like 'path/to/file.py::function_name'.
    We extract just 'function_name' for display. Falls back to the
    caller's file basename if symbol extraction fails.
    """
    names: list[str] = []
    for c in callers[:max_items]:
        name = _extract_symbol_from_snippet(c.snippet)
        if name is None:
            # Fall back to file basename — cleaner than raw snippet words.
            name = c.file.rsplit("/", 1)[-1].removesuffix(".py")
        names.append(name)
    return ", ".join(names)


def _generate_suggestions(
    parsed: ParsedQuery,
    selection: Selection | None,
) -> tuple[str, ...] | None:
    """Generate contextual suggestions when a query returns no results.

    Uses the synonym map for 'did you mean X?' hints and flags when the
    selection may have over-constrained the search.
    """
    hints: list[str] = []
    # Synonym suggestions: for each keyword, suggest a related term.
    if parsed.keywords:
        seen_syns: set[str] = set()
        for kw in parsed.keywords[:3]:
            for syn in SYNONYM_MAP.get(kw, ()):
                if syn not in seen_syns and syn not in parsed.keywords:
                    hints.append(f"Did you mean: {syn}?")
                    seen_syns.add(syn)
                    if len(hints) >= 2:
                        break
            if len(hints) >= 2:
                break
    # Selection hint: only suggest this when the selection was actually used
    # for symbol extraction (not just present but irrelevant).
    if selection and selection.text and parsed.symbol:
        hints.append("Try searching without the selection")
    return tuple(hints) if hints else None


def _gen_grep_patterns(symbol: str, intent: str) -> list[str]:
    """Generate grep patterns for a symbol based on intent.

    Algorithmic, not hard-coded: takes intent→pattern templates and
    interpolates the symbol.  E.g. 'definition' + 'pascal_case' →
    ['def\\s+pascal_case\\b', 'class\\s+pascal_case\\b', ...].
    """
    templates = _INTENT_PATTERNS.get(intent, [r"{symbol}\b"])
    escaped = re.escape(symbol)
    return [t.format(symbol=escaped) for t in templates]


def _read_line(file: str, line: int, workspace: str) -> str:
    """Read a specific line from a file, or return file:line if unreadable."""
    from pathlib import Path

    # Always resolve workspace to absolute so relative paths work.
    path = Path(workspace).resolve() / file
    if not path.exists():
        return f"{file}:{line}"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1][:300]
    except (OSError, UnicodeDecodeError):
        pass
    return f"{file}:{line}"
