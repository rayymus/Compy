"""CLI entrypoint — reads a JSON `QueryRequest`, runs the pipeline, writes a JSON `QueryResult`.

Designed for two callers:
  - The Swift overlay during development (`python -m compy.daemon < request.json`).
  - Tests, which feed a file via `--reasoner stub` to bypass the real backends.

The default reasoner chain is `freebuff -> ollama -> stub` per spec §5d; tests can override
with `--reasoner {freebuff,ollama,stub}` to pin a single backend (always plus the always-
succeeding stub at the tail so the chain terminates).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .gitlog import GitHistory
from .graphify import GraphQuerier
from .grepper import RipgrepGrepper
from .heuristic_reasoner import HeuristicReasoner
from .models import QueryRequest, to_json
from .orchestrator import run as run_pipeline
from .parser import RuleBasedParser
from .reasoner import FreebuffReasoner, OllamaReasoner, StubReasoner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="compy.daemon",
        description="Compy code-search pipeline (parse -> grep -> reason).",
    )
    parser.add_argument(
        "input", nargs="?", default=None,
        help="Path to JSON QueryRequest. If omitted, read from stdin.",
    )
    parser.add_argument(
        "--reasoner",
        choices=("freebuff", "ollama", "heuristic", "stub"),
        default=None,
        help="Pin the reasoner chain to a single backend. Default chain is "
             "freebuff -> ollama -> heuristic -> stub.",
    )
    parser.add_argument(
        "--rg", default="rg",
        help="Path to ripgrep binary (for the RipgrepGrepper).",
    )
    parser.add_argument(
        "--graph-rebuild",
        action="store_true",
        default=False,
        help="Force rebuild of the code-knowledge graph (Graphify).",
    )
    args = parser.parse_args(argv)

    try:
        request = _read_request(args.input)
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    reasoners = _build_reasoners(args.reasoner)

    # Build Graphify querier — always initialized so overview/explain/rename/
    # dead_code queries work. Uses fast_only=True by default (loads cached graph
    # without rebuilding). Only force-rebuilds when --graph-rebuild is passed.
    # If no cache exists, grapher exists but queries gracefully return empty.
    grapher: GraphQuerier | None = None
    historian: GitHistory | None = None
    if request.selection:
        ws = request.selection.workspace_root or "."
    else:
        ws = "."

    try:
        grapher = GraphQuerier()
        grapher.load(ws, force_rebuild=args.graph_rebuild, fast_only=not args.graph_rebuild)
    except Exception:
        grapher = None  # Graph not available — overview/explain/rename fall through.

    historian = GitHistory()

    # Streaming callback: run heuristic ranker on candidates immediately so
    # the overlay shows properly scored results before Ollama returns.
    # This eliminates the 500ms-2s Ollama delay from perceived latency —
    # users see instant ranked results (heuristic, ~10ms) while Ollama runs.
    # When Ollama finishes, the final result overrides with LLM-quality ranking.
    _heuristic = HeuristicReasoner()

    def _stream_candidates(candidates: "tuple[GrepHit, ...]") -> None:
        if not request.stream or not candidates:
            return
        # Run heuristic ranker for instant scores (10ms, offline).
        ranked = _heuristic.reason(request.question, candidates)
        hits = [{
            "file": h.file, "line": h.line, "snippet": h.snippet,
            "score": round(h.score, 3), "source": h.source,
        } for h in ranked]
        print(json.dumps({
            "stream": "candidates", "hits": hits, "count": len(candidates),
        }), flush=True)

    result = run_pipeline(
        request,
        parser=RuleBasedParser(),
        grepper=RipgrepGrepper(rg_path=args.rg),
        reasoners=reasoners,
        grapher=grapher,
        historian=historian,
        on_candidates=_stream_candidates,
    )
    print(to_json(result))
    return 0


def _read_request(source: str | None) -> QueryRequest:
    if source and source != "-":
        with open(source) as f:
            data: dict[str, Any] = json.load(f)
    else:
        data = json.load(sys.stdin)
    return QueryRequest.from_dict(data)


def _build_reasoners(pinned: str | None) -> tuple:
    """Default chain: freebuff -> ollama -> heuristic -> stub.

    The chain always terminates with StubReasoner because it's the only adapter guaranteed
    to never raise ReasonerUnavailable — that guarantee is what keeps the orchestrator from
    leaking exceptions (spec §3a "never a hard error").

    HeuristicReasoner is the v1 "works offline" fallback that does keyword-overlap scoring
    — it makes results genuinely useful without any LLM dependency.

    When `--reasoner stub` is pinned, the chain is *just* a single StubReasoner — there is
    no reason to tack another StubReasoner on its tail (the user explicitly asked for stub
    and it already succeeds by definition).

    Set COMPY_SKIP_FREEBUFF=1 to omit FreebuffReasoner from the default chain entirely.
    Freebuff CLI v0.0.122 has no -p flag, so every query pays ~200-500ms of wasted
    subprocess spawn + error before falling through. This env var skips that waste.
    """
    import os
    skip_freebuff = os.environ.get("COMPY_SKIP_FREEBUFF") == "1"
    # HeuristicReasoner is appended as the offline safety tail below —
    # including it in the head as well would double-invoke it per query.
    default_chain = (
        (OllamaReasoner(),)
        if skip_freebuff
        else (FreebuffReasoner(), OllamaReasoner())
    )
    chains = {
        None: default_chain,
        "freebuff": (FreebuffReasoner(),),
        "ollama": (OllamaReasoner(),),
        "heuristic": (HeuristicReasoner(),),
        "stub": (StubReasoner(),),
    }
    head = chains[pinned]
    if pinned == "stub" or pinned == "heuristic":
        return head
    return head + (HeuristicReasoner(), StubReasoner())
