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
    """A single line-level hit from ripgrep."""

    file: str
    line: int
    column: int
    snippet: str
    symbol: str | None = None  # populated when we know which token matched


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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QueryRequest:
        sel_raw = data.get("selection")
        selection = Selection(**sel_raw) if sel_raw else None
        return cls(
            question=data["question"],
            selection=selection,
            stream=data.get("stream", False),
        )


@dataclass(frozen=True)
class QueryResult:
    """Daemon output — what the overlay renders."""

    intent: str
    hits: tuple[RankedHit, ...] = field(default_factory=tuple)
    degraded: bool = False
    reason: str | None = None  # human-readable explanation when degraded=True
    suggestions: tuple[str, ...] | None = None  # "did you mean X?" on no-match


def to_json(obj: Any) -> str:
    """Stable JSON serialization for dataclasses — compact (single-line) so the
    overlay can split daemon stdout by newlines to separate stream events from
    the final QueryResult.  Handles tuples via default=str."""
    return json.dumps(asdict(obj), default=str)
