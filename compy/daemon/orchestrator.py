"""Pipeline orchestrator: parse -> grep -> reason, with cascading fallback.

Per spec §3a, the result panel must NEVER present a hard error — every failure degrades
into either "grep-only hits" or an empty "no results" hint state. This module reads
exactly that way: no raised exceptions escape.
"""

from __future__ import annotations

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
    )
    return QueryResult(intent=parsed.intent, hits=hits, degraded=degraded, reason=reason)


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
) -> tuple[ParsedQuery, tuple[RankedHit, ...], bool, str | None]:
    sel_file = selection.file if selection else None
    sel_text = selection.text if selection else None

    # --- Graphify path: relational queries ("what calls X", "who imports Y") ---
    if parsed.intent == "relational" and grapher is not None:
        try:
            grapher.load(workspace)
        except ReasonerUnavailable:
            pass  # Fall through — graph unavailable, try fuzzy grep.
        else:
            # "callers of" / "who calls" → callers; "what does X call" → calls
            if ("callers of" in question.lower() or "who calls" in question.lower()):
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
                    )
        # Graph returned empty — fall through to fuzzy grep.
        parsed = replace(parsed, intent="fuzzy")

    # --- Git history path: historical queries ("why was X changed") ---
    if parsed.intent == "history" and historian is not None:
        candidates = historian.query_history(question, workspace)
        if not candidates and selection:
            candidates = historian.query_file_history(sel_file or "", workspace)
        if candidates:
            return _rank_or_degrade(
                parsed=parsed, reasoners=reasoners,
                question=question, candidates=candidates,
                selection_file=sel_file, selection_text=sel_text,
            )
        # No git history found — fall through to fuzzy grep.
        parsed = replace(parsed, intent="fuzzy")

    # Confidence-too-low drops into fuzzy (spec §2a).
    if parsed.confidence < FUZZY_THRESHOLD:
        parsed = replace(parsed, intent="fuzzy")

    # Structured path: parse a symbol, search it.
    if parsed.intent in ("references", "definition") and parsed.symbol:
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
            )

    # Fuzzy path: try each parsed keyword as a grep query.
    if parsed.intent == "fuzzy":
        candidates: tuple[GrepHit, ...] = ()
        for kw in list(parsed.keywords)[:MAX_FUZZY_KEYWORD_TRIES]:
            try:
                candidates = grepper.grep(kw, workspace)
            except ReasonerUnavailable as exc:
                return parsed, (), True, f"grep failed: {exc}"
            if candidates:
                break
        if not candidates:
            return parsed, (), False, "no hits"

        return _rank_or_degrade(
            parsed=parsed, reasoners=reasoners,
            question=question, candidates=candidates,
            selection_file=sel_file, selection_text=sel_text,
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
) -> tuple[ParsedQuery, tuple[RankedHit, ...], bool, str | None]:
    """Try the reasoner chain. Every failure falls through, all-failure degrades to grep hint."""
    last_err: str | None = None
    for r in reasoners:
        try:
            ranked = r.reason(question, candidates,
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
        )
        for i, h in enumerate(hits)
    )


def _selection_text(sel: Selection | None) -> str | None:
    return sel.text if sel and sel.text else None


def _first_keyword(keywords: tuple[str, ...]) -> str | None:
    return keywords[0] if keywords else None
