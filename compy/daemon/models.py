"""Data classes for the daemon's input / output contracts.

Pure stdlib dataclasses — no pydantic. Each is `frozen=True` so the orchestrator can hold
references across the pipeline without anyone mutating in place.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Selection:
    """Code selection carried with the query.

    `text` is the snippet; `file`/`line` come from the VS Code-API companion extension (spec
    §8c) or from a clipboard fallback. `workspace_root` is what grep needs to scope correctly.
    """

    text: str
    file: str | None = None
    line: int | None = None
    workspace_root: str | None = None


@dataclass(frozen=True)
class ParsedQuery:
    """Step-1 output: structured intent extracted from the question + selection."""

    intent: str  # one of: "references", "definition", "fuzzy"
    symbol: str | None
    keywords: tuple[str, ...]
    confidence: float  # 0.0–1.0; below FUZZY_THRESHOLD drops into fuzzy mode per spec §2a


@dataclass(frozen=True)
class GrepHit:
    """A single line-level hit from ripgrep.

    `context` is an optional structural annotation like "Callers: login, auth_mw" —
    computed from Graphify/gitlog before the reasoner ranks, so even a small local
    model can leverage the code graph rather than guessing from bare text.
    """

    file: str
    line: int
    column: int
    snippet: str
    symbol: str | None = None  # populated when we know which token matched
    context: str | None = None  # structural annotation for reasoner enrichment


@dataclass(frozen=True)
class RankedHit:
    """Final ranked result the overlay displays. `source` tells tests/UI where it came from.

    `structural_context` is an optional badge string like "Called by: login_handler, auth_mw"
    populated from Graphify after ranking — shows callers/importers for the hit's symbol.
    """

    file: str
    line: int
    snippet: str
    score: float
    source: str  # "grep" | "freebuff" | "ollama" | "stub"
    structural_context: str | None = None


@dataclass(frozen=True)
class QueryRequest:
    """Daemon input — what the Swift overlay or VS Code extension sends over the wire."""

    question: str
    selection: Selection | None = None
    stream: bool = False  # when True, daemon emits intermediate candidates before ranking
    session_context: tuple[str, ...] | None = None  # previous turn's hits for follow-up queries

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueryRequest:
        sel_raw = data.get("selection")
        selection = Selection(**sel_raw) if sel_raw else None
        ctx_raw = data.get("session_context")
        session_context = tuple(ctx_raw) if isinstance(ctx_raw, list) else None
        return cls(
            question=data["question"],
            selection=selection,
            stream=data.get("stream", False),
            session_context=session_context,
        )


@dataclass(frozen=True)
class QueryResult:
    """Daemon output — what the overlay renders."""

    intent: str
    hits: tuple[RankedHit, ...] = field(default_factory=tuple)
    degraded: bool = False
    reason: str | None = None  # human-readable explanation when degraded=True
    suggestions: tuple[str, ...] | None = None  # "did you mean X?" on no-match
    # Refactoring pipeline — set when intent="format" and proposals are staged.
    refactor_proposals: tuple[FileProposal, ...] | None = None  # files that would change
    refactor_token: str | None = None  # pointer to staged edits on disk for /confirm
    # Working Set Engine — Session 34
    next_questions: tuple[str, ...] | None = None  # "X is called in N places — see them?"
    personalization_active: bool = False  # True when ranking was biased by recent context


@dataclass(frozen=True)
class FileProposal:
    """A single file change proposal in a refactoring operation.

    Lightweight — just the file path and a change summary.  Full diffs are
    reviewed in the editor, not rendered inside the overlay.
    """

    file: str
    changed_lines: int  # approximate — lines added + removed


def to_json(obj: Any) -> str:
    """Stable JSON serialization for dataclasses — compact (single-line) so the
    overlay can split daemon stdout by newlines to separate stream events from
    the final QueryResult.  Handles tuples via default=str."""
    return json.dumps(asdict(obj), default=str)
