"""Step-3 Reasoner adapters.

Per spec §5d, the daemon ships behind one `Reasoner` Protocol. Three implementations:

  - `FreebuffReasoner` — best-effort subprocess to the user's local `freebuff` CLI.
  - `OllamaReasoner` — HTTP POST to a local Ollama daemon.
  - `StubReasoner` — deterministic, used in tests; never raises.

If `freebuff`/`ollama` are not wired up correctly, the adapter raises `ReasonerUnavailable`
and the orchestrator falls through to the next reasoner in the chain. The spec's contract
is automatic degradation, not hard errors.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from typing import Any

from .interfaces import ReasonerUnavailable
from .models import GrepHit, RankedHit


# Match bracketed candidate references like "[2]". We trust the reasoner to emit this
# shape; if it produces unstructured prose we degrade (see `_interpret_indexed_response`).
# Earlier we used `^\s*\[?\s*(\d+)\s*\]?` with re.MULTILINE, which fails on inputs like
# `"best is [2]\nthen [0]\n"` because the digits aren't at line start.
_INDEX_RE = re.compile(r"\[(\d+)\]")


class FreebuffReasoner:
    """Best-effort Freebuff CLI invocation.

    The spec's §5d primary reasoner. At the time of writing this slice, the installed freebuff
    CLI v0.0.122 exposes only `--continue [id]`, `--cwd`, `--version`, and the `login`
    subcommand — no `-p` or stdin prompt mode (verified directly via `freebuff --help`).

    This adapter ships the contract anyway: it tries `freebuff -p <question>` and reads
    candidates on stdin, but raises `ReasonerUnavailable` cleanly when the real CLI rejects
    the flag. On the user's production machine, this gets wired to a wrapping layer (PT-style
    session, direct call into the Node SDK at `/opt/homebrew/lib/node_modules/freebuff`, etc.)
    — the orchestrator's fallback chain already handles the unavailable case.
    """

    def __init__(self, freebuff_path: str = "freebuff", timeout_s: float = 25.0) -> None:
        self._path = freebuff_path
        self._timeout = timeout_s

    @property
    def name(self) -> str:
        return "freebuff"

    def reason(self, question: str, candidates: tuple[GrepHit, ...], *, selection_file: str | None = None, selection_text: str | None = None) -> tuple[RankedHit, ...]:
        _ = (selection_file, selection_text)  # unused — Freebuff gets context via the prompt
        try:
            proc = subprocess.run(
                [self._path, "-p", question],
                input=_prompt(question, candidates),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ReasonerUnavailable(f"freebuff CLI not found at '{self._path}'") from exc
        except subprocess.TimeoutExpired as exc:
            raise ReasonerUnavailable(f"freebuff CLI timed out after {exc.timeout}s") from exc
        except OSError as exc:
            raise ReasonerUnavailable(f"freebuff CLI OS error: {exc}") from exc

        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            # The "unrecognized argument" message is the current real behavior;
            # surface it cleanly so the orchestrator's fallback message is informative.
            raise ReasonerUnavailable(
                f"freebuff CLI exited {proc.returncode}: {stderr[:160] or '(no stderr)'}"
            )

        # If a future Freebuff mode returns a parseable ranked response, this dimly detects
        # index-bracketed ordering (`\n[3]\n[1]\n...`). Real impl: replace with whatever
        # schema Freebuff emits when non-interactive mode ships.
        return _interpret_indexed_response(proc.stdout, candidates, source="freebuff")


class OllamaReasoner:
    """HTTP backend against a local Ollama instance (default port 11434).

    Model is read from COMPY_OLLAMA_MODEL env var; falls back to qwen2.5-coder:1.5b
    (small enough for 16GB unified memory per spec §8d).
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str = "http://localhost:11434",
        timeout_s: float = 30.0,
    ) -> None:
        import os
        self._model = model or os.environ.get("COMPY_OLLAMA_MODEL", "qwen2.5-coder:1.5b")
        self._url = f"{base_url}/api/generate"
        self._timeout = timeout_s

    @property
    def name(self) -> str:
        return "ollama"

    def reason(self, question: str, candidates: tuple[GrepHit, ...], *, selection_file: str | None = None, selection_text: str | None = None) -> tuple[RankedHit, ...]:
        _ = (selection_file, selection_text)  # unused — Ollama gets context via the prompt
        body = json.dumps({
            "model": self._model,
            "prompt": _ollama_prompt(question, candidates),
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ReasonerUnavailable(f"ollama unreachable at {self._url}: {exc.reason}") from exc
        except urllib.error.HTTPError as exc:
            raise ReasonerUnavailable(
                f"ollama HTTP {exc.code}: {exc.reason}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ReasonerUnavailable(f"ollama response malformed: {exc}") from exc

        return _interpret_indexed_response(
            payload.get("response", ""), candidates, source="ollama"
        )


class StubReasoner:
    """Deterministic test reasoner — ranks by index order with optional score override.

    Three failure-mode flags for testing the orchestrator's fallback chain:
      - `raises=True`        — raises `ReasonerUnavailable` (treated like a real backend
                                outage; orchestrator falls through).
      - `empty_returns=True` — returns `()` (treated like "succeeded but had nothing to
                                rank"; orchestrator falls through).
      - default              — returns N RankedHits ranked by index, scores 1/(i+1).
    """

    def __init__(
        self,
        scores: tuple[float, ...] | None = None,
        raises: bool = False,
        empty_returns: bool = False,
    ) -> None:
        self._scores = scores
        self._raises = raises
        self._empty = empty_returns
        self.calls: list[tuple[str, tuple[GrepHit, ...]]] = []

    @property
    def name(self) -> str:
        return "stub"

    def reason(self, question: str, candidates: tuple[GrepHit, ...], *, selection_file: str | None = None, selection_text: str | None = None) -> tuple[RankedHit, ...]:
        _ = (selection_file, selection_text)  # unused — stub is deterministic
        self.calls.append((question, candidates))
        if self._raises:
            raise ReasonerUnavailable("stub: failure injection")
        if self._empty:
            return ()  # explicit empty return — orchestrator should fall through.
        n = len(candidates)
        if self._scores is None:
            scores = [1.0 / (i + 1) for i in range(n)]
        else:
            scores = list(self._scores)[:n] + [0.0] * max(0, n - len(self._scores))
        return tuple(
            RankedHit(
                file=c.file, line=c.line, snippet=c.snippet,
                score=scores[i], source="stub",
                structural_context=c.context,
            )
            for i, c in enumerate(candidates)
        )


def _prompt(question: str, candidates: tuple[GrepHit, ...]) -> str:
    """Shared prompt shape: question first, then bracketed candidate list.

    Designed so the candidate snippets get pasted into Freebuff's prompt content directly
    — this is the §8a mitigation for "Freebuff's file-picker subagent would otherwise
    re-scan the repo and we'd pay the blind whole-repo agent cost."

    Each candidate line now includes structural context when available (Layer 2 enrichment).
    """
    rows = "\n".join(
        _format_candidate_line(i, c) for i, c in enumerate(candidates)
    )
    return (
        f"{question}\n\n"
        f"Candidates ({len(candidates)}; pick the best by index — output indices in order):\n"
        f"{rows}"
    )


def _ollama_prompt(question: str, candidates: tuple[GrepHit, ...]) -> str:
    rows = "\n".join(
        _format_candidate_line(i, c, max_snippet=200) for i, c in enumerate(candidates)
    )
    return (
        f"Question: {question}\n\n"
        f"Candidates:\n{rows}\n\n"
        f"Rank candidates by relevance. Output one bracketed index per line, best first."
    )


def _format_candidate_line(i: int, c: GrepHit, *, max_snippet: int = 140) -> str:
    """Format a single candidate line for the reasoner prompt.

    Includes structural context (callers, verification) when the orchestrator
    has enriched the GrepHit via _enrich_candidates_for_ranking.
    """
    ctx = ""
    if c.context:
        ctx = f"  [{c.context}]"
    return f"[{i}] {c.file}:{c.line}{ctx}\n{c.snippet[:max_snippet]}"


def _interpret_indexed_response(
    response: str,
    candidates: tuple[GrepHit, ...],
    *,
    source: str,
) -> tuple[RankedHit, ...]:
    """Naive parse: pick up whatever `[N]` indices appear in the response.

    Indices are preserved in *first-seen* order — that's the model's emitted ranking.
    Coverage rule (per code-review recommendation): if the response ranks fewer than half
    of the candidates, treat as unparseable and fall back to the grep order with descending
    scores instead of silently dropping the un-ranked majority. (Earlier we sorted the
    indices numerically here, which gave the wrong answer when the response emitted e.g.
    `[2]\n[0]\n[1]` — that ordering is unambiguous as a preference list but numerically
    sorts to `[0,1,2]`, losing the model's preference order.)
    """
    raw: list[int] = []
    seen: set[int] = set()
    for match in _INDEX_RE.finditer(response):
        idx = int(match.group(1))
        if 0 <= idx < len(candidates) and idx not in seen:
            raw.append(idx)
            seen.add(idx)
    indexed = raw  # first-seen order from the response is the model's ranking.

    if not indexed or len(indexed) < max(2, len(candidates) // 2):
        # Reasoner didn't emit enough — degrade gracefully: preserve original
        # grep order with monotonic descending scores. Spec-friendly: never return empty
        # when there are candidates to rank.
        return tuple(
            RankedHit(
                file=c.file, line=c.line, snippet=c.snippet,
                score=1.0 / (i + 1), source=source,
                structural_context=c.context,
            )
            for i, c in enumerate(candidates)
        )

    return tuple(
        RankedHit(
            file=candidates[idx].file,
            line=candidates[idx].line,
            snippet=candidates[idx].snippet,
            score=1.0 / (rank + 1),
            source=source,
            structural_context=candidates[idx].context,
        )
        for rank, idx in enumerate(indexed)
    )
