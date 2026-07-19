"""Working Set Engine — session-scoped activation scores + Personalized PageRank.

Named after the OS concept: the set of pages a process is actively using right now.
Instead of ranking every query cold, maintain a live, decaying model of what's
contextually active this session, personalize ranking toward it, and let ambient
badges and next-question suggestions fall out of it for free.

Three outputs from one mechanism:
  1. Ranking bias       — re-weight ranked hits toward active graph nodes
  2. Ambient badges      — structural_context already rendered by Swift
  3. Next-question       — "X is called in N places — see them?"

Six failure modes mitigated (per working-set-plan.html):
  - Cold start          → no activation → skip bias (unpersonalized fallback)
  - Latency             → incremental decay on touched nodes, never full recompute
  - Tunnel vision       → topic-shift detector resets on low keyword overlap
  - Feedback poisoning  → session-scoped, decaying, never cross-session
  - Workspace bleed     → tmp file is workspace-hash scoped
  - Silent inconsistency → personalization_active flag surfaced to UI

State lives in /tmp/compy-workingset-{hash}.json (daemon-owned, ephemeral process).
Click signal lives in /tmp/compy-workingset-click-{hash}.json (Swift writes on click).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import networkx as nx
    from .models import RankedHit

# ─── Tuning constants ──────────────────────────────────────────────────────

DECAY_FACTOR = 0.7          # scores fade ~5 turns: 0.7^5 ≈ 0.17
CLICK_BOOST = 1.0           # clicked node gets full boost
NEIGHBOR_BOOST = 0.5        # immediate callers/callees get half
QUERY_BOOST = 0.3           # every result node gets a lighter boost
MIN_SCORE = 0.01            # prune below this after decay
TOPIC_SHIFT_THRESHOLD = 0.15  # Jaccard overlap below this → reset
MAX_RECENT_KEYWORDS = 20    # keep last N keywords for topic-shift detection
MAX_NEXT_QUESTIONS = 3
PR_ALPHA = 0.85             # PageRank damping factor
BIAS_BLEND = 0.2            # 80% original score + 20% activation bias
NODE_LINE_TOLERANCE = 5     # lines of slack when mapping hit→graph node


# ─── Path helpers ──────────────────────────────────────────────────────────

def _ws_path(workspace: str) -> Path:
    h = hashlib.md5(workspace.encode()).hexdigest()[:8]
    return Path(f"/tmp/compy-workingset-{h}.json")


def _click_path(workspace: str) -> Path:
    h = hashlib.md5(workspace.encode()).hexdigest()[:8]
    return Path(f"/tmp/compy-workingset-click-{h}.json")


def _norm_file(file: str, workspace: str) -> str:
    """Normalize a file path to a workspace-relative string without ./ prefix."""
    if os.path.isabs(file):
        try:
            file = os.path.relpath(file, workspace)
        except ValueError:
            pass
    return file.removeprefix("./")


# ─── WorkingSet ────────────────────────────────────────────────────────────

class WorkingSet:
    """Session-scoped activation scores with decay and Personalized PageRank bias.

    Activation scores are keyed by ``"rel_path:line"`` (human-facing, matches
    RankedHit fields). The graph structure is only used when computing the
    PageRank bias — graceful degradation to raw scores when graph is unavailable.
    """

    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.normpath(workspace)
        self._activation: dict[str, float] = {}
        self._recent_keywords: list[str] = []
        self._turn_count: int = 0
        # Cache: {(file, line): node_id} built lazily by _resolve_node_cached.
        self._node_index: dict[tuple[str, int], str] | None = None

    # ── Persistence ────────────────────────────────────────────────────────

    @classmethod
    def load(cls, workspace: str) -> WorkingSet:
        """Load from tmp file, or create empty (cold start)."""
        ws = cls(workspace)
        try:
            data = json.loads(_ws_path(workspace).read_text("utf-8"))
            ws._activation = {
                k: float(v) for k, v in data.get("activation", {}).items()
            }
            ws._recent_keywords = list(data.get("recent_keywords", []))
            ws._turn_count = int(data.get("turn_count", 0))
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            pass  # cold start — empty working set
        return ws

    def save(self) -> None:
        """Write to tmp file. Best-effort — never blocks the query."""
        try:
            _ws_path(self.workspace).write_text(
                json.dumps({
                    "workspace": self.workspace,
                    "activation": self._activation,
                    "recent_keywords": self._recent_keywords[-MAX_RECENT_KEYWORDS:],
                    "turn_count": self._turn_count,
                }),
                "utf-8",
            )
        except OSError:
            pass

    # ── Click signal ───────────────────────────────────────────────────────

    def consume_click(self, graph: nx.DiGraph | None) -> None:
        """Read and apply a pending click from the tmp file, then delete it.

        Boosts the clicked node + immediate graph neighbors (callers/callees).
        """
        try:
            data = json.loads(_click_path(self.workspace).read_text("utf-8"))
            file = _norm_file(data.get("file", ""), self.workspace)
            line = int(data.get("line", 0))
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            return  # no click, malformed, or missing line — skip silently
        if not file or line <= 0:
            return
        key = f"{file}:{line}"
        self._activation[key] = self._activation.get(key, 0) + CLICK_BOOST

        # Propagate to immediate graph neighbors if graph is available.
        if graph is not None:
            node_id = self._resolve_node_cached(file, line, graph)
            if node_id:
                for nbr in graph.predecessors(node_id):
                    nbr_file = graph.nodes[nbr].get("file", "")
                    nbr_line = graph.nodes[nbr].get("line", 0)
                    if nbr_file and nbr_line:
                        nk = f"{nbr_file}:{nbr_line}"
                        self._activation[nk] = self._activation.get(nk, 0) + NEIGHBOR_BOOST
                for nbr in graph.successors(node_id):
                    nbr_file = graph.nodes[nbr].get("file", "")
                    nbr_line = graph.nodes[nbr].get("line", 0)
                    if nbr_file and nbr_line:
                        nk = f"{nbr_file}:{nbr_line}"
                        self._activation[nk] = self._activation.get(nk, 0) + NEIGHBOR_BOOST

        # Click consumed — remove the signal file.
        try:
            _click_path(self.workspace).unlink()
        except FileNotFoundError:
            pass

    # ── Decay ──────────────────────────────────────────────────────────────

    def decay(self) -> None:
        """Multiply all scores by DECAY_FACTOR, prune below MIN_SCORE."""
        self._activation = {
            k: v * DECAY_FACTOR
            for k, v in self._activation.items()
            if v * DECAY_FACTOR >= MIN_SCORE
        }

    # ── Topic-shift detection ──────────────────────────────────────────────

    def detect_topic_shift(self, keywords: tuple[str, ...]) -> bool:
        """Return True if the new query's keywords have low overlap with recent."""
        if not self._recent_keywords or not keywords:
            return False  # first query or no keywords → no shift
        recent_set = set(self._recent_keywords)
        new_set = set(keywords)
        union = recent_set | new_set
        if not union:
            return False
        overlap = len(recent_set & new_set) / len(union)
        return overlap < TOPIC_SHIFT_THRESHOLD

    def record_keywords(self, keywords: tuple[str, ...]) -> None:
        """Add this query's keywords to recent (for future topic-shift checks)."""
        self._recent_keywords.extend(keywords)
        self._recent_keywords = self._recent_keywords[-MAX_RECENT_KEYWORDS:]

    def reset(self) -> None:
        """Topic-shift reset — clear activation scores, keep turn count."""
        self._activation = {}
        self._recent_keywords = []

    # ── Query recording ────────────────────────────────────────────────────

    def record_query(self, hits: tuple[RankedHit, ...]) -> None:
        """Boost activation for all result nodes (lighter than click boost)."""
        for hit in hits:
            file = _norm_file(hit.file, self.workspace)
            key = f"{file}:{hit.line}"
            self._activation[key] = self._activation.get(key, 0) + QUERY_BOOST
        self._turn_count += 1

    # ── Graph node resolution ──────────────────────────────────────────────

    @staticmethod
    def _resolve_node(
        file: str, line: int, graph: nx.DiGraph
    ) -> str | None:
        """Map a (file, line) to the closest graph node ID.

        Graph nodes are ``"rel_path::symbol"`` with ``file`` and ``line`` attrs.
        Returns the closest node within NODE_LINE_TOLERANCE lines, or None.
        """
        best: tuple[str, int] | None = None
        for nid, attrs in graph.nodes(data=True):
            if attrs.get("file") != file:
                continue
            dist = abs(attrs.get("line", 0) - line)
            if dist <= NODE_LINE_TOLERANCE and (best is None or dist < best[1]):
                best = (nid, dist)
        return best[0] if best else None

    def _resolve_node_cached(
        self, file: str, line: int, graph: nx.DiGraph
    ) -> str | None:
        """Cached version of _resolve_node — builds a file+line index once.

        O(N) to build the index, then O(1) lookups. Essential for large graphs
        where _resolve_node is called many times (per hit + per activation entry).
        """
        if self._node_index is None:
            idx: dict[tuple[str, int], str] = {}
            for nid, attrs in graph.nodes(data=True):
                f = attrs.get("file", "")
                ln = attrs.get("line", 0)
                if f and ln:
                    key = (f, ln)
                    if key not in idx:  # first node at this file:line wins
                        idx[key] = nid
            self._node_index = idx
        # Exact match first, then fuzzy within tolerance.
        if (file, line) in self._node_index:
            return self._node_index[(file, line)]
        best: tuple[str, int] | None = None
        for (f, ln), nid in self._node_index.items():
            if f != file:
                continue
            dist = abs(ln - line)
            if dist <= NODE_LINE_TOLERANCE and (best is None or dist < best[1]):
                best = (nid, dist)
        return best[0] if best else None

    # ── Personalized PageRank bias ─────────────────────────────────────────

    def compute_pagerank(self, graph: nx.DiGraph) -> dict[str, float]:
        """Run Personalized PageRank biased toward active nodes.

        Returns a ``{node_id: pr_score}`` dict, or empty if cold start or
        graph has no activation to bias toward.
        """
        if not self._activation or graph.number_of_nodes() == 0:
            return {}

        # Build personalization dict: map file:line → node_id, accumulate scores.
        pers: dict[str, float] = {}
        for key, score in self._activation.items():
            file, _, line_str = key.rpartition(":")
            try:
                line = int(line_str)
            except ValueError:
                continue
            node_id = self._resolve_node_cached(file, line, graph)
            if node_id:
                pers[node_id] = pers.get(node_id, 0) + score

        if not pers:
            return {}

        # Normalize so values sum to 1.0 (required by nx.pagerank).
        total = sum(pers.values())
        if total <= 0:
            return {}
        pers = {k: v / total for k, v in pers.items()}

        try:
            import networkx as nx_mod
            return nx_mod.pagerank(graph, personalization=pers, alpha=PR_ALPHA)
        except Exception:
            return {}  # graph too small, convergence failure, etc. — skip bias

    # ── Hit biasing ────────────────────────────────────────────────────────

    def bias_hits(
        self,
        hits: tuple[RankedHit, ...],
        graph: nx.DiGraph | None,
    ) -> tuple[tuple[RankedHit, ...], bool]:
        """Re-weight ranked hits toward active nodes.

        Returns (biased_hits, personalization_active).
        If graph is available, uses Personalized PageRank (graph-propagated).
        Otherwise falls back to raw activation scores (no propagation).
        If no activation exists (cold start), returns hits unchanged.
        """
        if not self._activation or not hits:
            return hits, False

        if graph is not None and graph.number_of_nodes() > 0:
            return self._bias_with_graph(hits, graph)
        return self._bias_raw(hits)

    def _bias_with_graph(
        self, hits: tuple[RankedHit, ...], graph: nx.DiGraph
    ) -> tuple[tuple[RankedHit, ...], bool]:
        pr = self.compute_pagerank(graph)
        if not pr:
            return self._bias_raw(hits)

        # Find the max PR score for normalization.
        max_pr = max(pr.values()) if pr else 0
        if max_pr <= 0:
            return self._bias_raw(hits)

        biased: list[RankedHit] = []
        personalized = False
        for hit in hits:
            file = _norm_file(hit.file, self.workspace)
            node_id = self._resolve_node_cached(file, hit.line, graph)
            pr_score = pr.get(node_id, 0) if node_id else 0
            if pr_score > 0:
                personalized = True
                # Scale PR to 0–BIAS_BLEND range, blend with original score.
                bias_component = (pr_score / max_pr) * BIAS_BLEND
                new_score = hit.score * (1 - BIAS_BLEND) + bias_component
            else:
                new_score = hit.score
            biased.append(replace(hit, score=min(new_score, 1.0)))

        biased.sort(key=lambda h: h.score, reverse=True)
        return tuple(biased), personalized

    def _bias_raw(
        self, hits: tuple[RankedHit, ...]
    ) -> tuple[tuple[RankedHit, ...], bool]:
        """Fallback: use raw activation scores without graph propagation."""
        biased: list[RankedHit] = []
        personalized = False
        max_act = max(self._activation.values()) if self._activation else 0
        if max_act <= 0:
            return hits, False

        for hit in hits:
            file = _norm_file(hit.file, self.workspace)
            key = f"{file}:{hit.line}"
            act = self._activation.get(key, 0)
            if act > MIN_SCORE:
                personalized = True
                bias_component = (act / max_act) * BIAS_BLEND
                new_score = hit.score * (1 - BIAS_BLEND) + bias_component
            else:
                new_score = hit.score
            biased.append(replace(hit, score=min(new_score, 1.0)))

        biased.sort(key=lambda h: h.score, reverse=True)
        return tuple(biased), personalized

    # ── Next-question suggestions ──────────────────────────────────────────

    def generate_next_questions(
        self, graph: nx.DiGraph | None
    ) -> list[str]:
        """Generate 0–3 next-question suggestions from top active nodes.

        Uses graph structure: caller count, callee count, dead-code hints.
        Returns empty if no activation or no graph.
        """
        if not self._activation or graph is None:
            return []

        # Sort active nodes by score, take top candidates.
        top = sorted(self._activation.items(), key=lambda x: x[1], reverse=True)
        questions: list[str] = []

        for key, score in top:
            if score < MIN_SCORE or len(questions) >= MAX_NEXT_QUESTIONS:
                break
            file, _, line_str = key.rpartition(":")
            try:
                line = int(line_str)
            except ValueError:
                continue
            node_id = self._resolve_node_cached(file, line, graph)
            if not node_id:
                continue
            name = node_id.rsplit("::", 1)[-1] if "::" in node_id else node_id
            callers = list(graph.predecessors(node_id))
            callees = list(graph.successors(node_id))

            if len(callers) > 5:
                questions.append(
                    f"{name} is called in {len(callers)} places — see them?"
                )
            elif len(callers) > 0:
                questions.append(f"Who calls {name}?")
            elif len(callers) == 0:
                questions.append(f"Is {name} still used?")

            if len(questions) >= MAX_NEXT_QUESTIONS:
                break
            if len(callees) > 3:
                questions.append(f"What does {name} call?")

        return questions[:MAX_NEXT_QUESTIONS]

    # ── Accessors ──────────────────────────────────────────────────────────

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def has_activation(self) -> bool:
        return bool(self._activation)


# ─── Swift click writer (called from Swift side, but documented here) ──────
#
# When the user clicks a result in the overlay, Swift writes a JSON file:
#   /tmp/compy-workingset-click-{hash}.json
#   {"file": "path/to/file.py", "line": 42}
#
# The next daemon spawn reads this via consume_click(), applies the boost,
# and deletes the file. This is the feedback loop: clicks → activation scores,
# session-scoped, decaying (failure mode 4 mitigation).
