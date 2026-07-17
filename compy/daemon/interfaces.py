"""Pluggable surfaces — Protocol classes the orchestrator depends on.

The orchestrator does NOT import concrete implementations; this keeps tests easy and lets
production swap adapters without touching pipeline logic (spec §5d "Reasoner abstraction").
"""

from __future__ import annotations

from typing import Protocol

from .models import GrepHit, ParsedQuery, RankedHit


class ReasonerUnavailable(Exception):
    """Raised when a Reasoner cannot serve a request right now.

    The orchestrator catches this and tries the next Reasoner in the chain. StubReasoner is
    expected to never raise this — it is the last-resort success path.
    """


class Parser(Protocol):
    def parse(self, question: str, selection_text: str | None) -> ParsedQuery: ...


class Grepper(Protocol):
    def grep(self, pattern: str, workspace_root: str) -> tuple[GrepHit, ...]: ...


class Grapher(Protocol):
    """Relational code graph querier — "what calls X", "what imports Y"."""
    def query(self, symbol: str, intent: str = "calls") -> tuple[GrepHit, ...]: ...

    def query_dead_code(self) -> tuple[GrepHit, ...]: ...

    def query_overview(self) -> tuple[GrepHit, ...]: ...

    def load(self, workspace_root: str, *, force_rebuild: bool = False, fast_only: bool = False) -> None: ...


class Historian(Protocol):
    """Git history querier — "why was X changed", "who added this"."""
    def query_history(self, question: str, workspace_root: str) -> tuple[GrepHit, ...]: ...

    def query_blame(self, file: str, line: int, workspace_root: str) -> tuple[GrepHit, ...]: ...

    def query_file_history(self, file: str, workspace_root: str) -> tuple[GrepHit, ...]: ...


class Reasoner(Protocol):
    def reason(
        self,
        question: str,
        candidates: tuple[GrepHit, ...],
        *,
        selection_file: str | None = None,
        selection_text: str | None = None,
    ) -> tuple[RankedHit, ...]: ...

    @property
    def name(self) -> str: ...
