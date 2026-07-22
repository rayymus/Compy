"""Embedding-based semantic ranker — cosine similarity via Ollama embeddings.

Uses nomic-embed-text (768-dim) to embed the query and each candidate snippet,
then ranks by cosine similarity. This catches semantic equivalence that token
overlap misses: "retry logic" matches "reconnect_with_backoff" even though they
share zero tokens.

Falls back gracefully: if Ollama is unreachable or embeddings fail, returns ()
so the reasoner chain falls through to HeuristicReasoner.

Architecture:
  - Embeds the question once (1 API call).
  - Embeds each candidate snippet (N API calls, capped at 20).
  - Computes cosine similarity via dot product on normalized vectors.
  - Blends embedding score with heuristic token-overlap score (60/40) so
    semantic relevance doesn't override exact-match signal entirely.

The blend ratio favors embeddings (0.6) because the whole point is semantic
recall, but keeps 0.4 token overlap so exact symbol matches still win when
they exist.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from typing import Any

from .interfaces import ReasonerUnavailable
from .models import GrepHit, RankedHit

_TOK_RE = re.compile(r"[a-z0-9_]{2,}")


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. 0.0 if either is zero-length."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingRanker:
    """Semantic ranker using Ollama embeddings. Implements the Reasoner Protocol.

    Embeds the question + each candidate snippet, ranks by cosine similarity
    blended with token overlap. Falls through (returns empty) on any failure
    so the chain degrades to HeuristicReasoner.
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str = "http://localhost:11434",
        timeout_s: float = 30.0,
        blend: float = 0.6,
    ) -> None:
        self._model = model or os.environ.get(
            "COMPY_EMBED_MODEL", "nomic-embed-text"
        )
        self._embed_url = f"{base_url}/api/embeddings"
        self._timeout = timeout_s
        self._blend = blend  # embedding weight; (1-blend) is token overlap.

    @property
    def name(self) -> str:
        return "embedding"

    # Cap on candidates to embed — each embedding is a separate HTTP call (~50ms),
    # so embedding 50 candidates would add 2.5s latency. The top 20 from grep order
    # is sufficient for semantic re-ranking; the rest keep their grep order.
    _MAX_EMBED_CANDIDATES = 20

    def reason(
        self,
        question: str,
        candidates: tuple[GrepHit, ...],
        *,
        selection_file: str | None = None,
        selection_text: str | None = None,
    ) -> tuple[RankedHit, ...]:
        if not candidates:
            return ()

        # Cap candidates to embed — rest keep grep order with descending scores.
        to_embed = candidates[: self._MAX_EMBED_CANDIDATES]
        remainder = candidates[self._MAX_EMBED_CANDIDATES :]

        # Embed the question once.
        q_vec = self._embed(question)
        if q_vec is None:
            # Ollama unreachable — let chain fall through.
            raise ReasonerUnavailable("embedding: Ollama embeddings endpoint unavailable")

        # Embed each candidate snippet (enriched with context).
        scored: list[tuple[int, float, float]] = []  # (idx, embed_score, token_score)
        for i, c in enumerate(to_embed):
            text = f"{c.snippet} {c.context or ''}"
            c_vec = self._embed(text)
            if c_vec is None:
                # One failed — can't rank this candidate. Give it 0.
                scored.append((i, 0.0, _token_overlap(question, text)))
            else:
                embed_score = max(0.0, _cosine(q_vec, c_vec))
                token_score = _token_overlap(question, text)
                scored.append((i, embed_score, token_score))

        # Add remainder candidates (beyond cap) with token-overlap-only scores.
        for j, c in enumerate(remainder):
            text = f"{c.snippet} {c.context or ''}"
            tok = _token_overlap(question, text)
            scored.append((len(to_embed) + j, 0.0, tok))

        # Blend: embedding * blend + token_overlap * (1-blend).
        # Normalize embedding scores to 0-1 relative to max for better spread.
        max_embed = max(s[1] for s in scored) if scored else 0.0
        max_token = max(s[2] for s in scored) if scored else 0.0

        results: list[tuple[int, float]] = []
        for idx, emb, tok in scored:
            norm_emb = emb / max_embed if max_embed > 0 else 0.0
            norm_tok = tok / max_token if max_token > 0 else 0.0
            blended = (norm_emb * self._blend) + (norm_tok * (1.0 - self._blend))
            results.append((idx, blended))

        # Sort by blended score descending.
        results.sort(key=lambda x: x[1], reverse=True)

        # Normalize final scores to 0-1 with the top hit at 1.0.
        top = results[0][1] if results else 0.0
        if top <= 0:
            # All zero scores — nothing semantic to offer. Fall through.
            return ()

        return tuple(
            RankedHit(
                file=candidates[idx].file,
                line=candidates[idx].line,
                snippet=candidates[idx].snippet,
                score=round(score / top, 3),
                source="embedding",
                structural_context=candidates[idx].context,
            )
            for idx, score in results
        )

    def _embed(self, text: str) -> list[float] | None:
        """Get embedding vector from Ollama. Returns None on failure."""
        # Truncate very long texts — embeddings have token limits and
        # code snippets rarely need more than 500 chars for semantic matching.
        prompt = text.strip()[:2000]
        if not prompt:
            return None

        body = json.dumps({
            "model": self._model,
            "prompt": prompt,
        }).encode("utf-8")

        req = urllib.request.Request(
            self._embed_url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError):
            return None

        vec = payload.get("embedding")
        if not isinstance(vec, list) or not vec:
            return None
        return [float(v) for v in vec]


def _token_overlap(query: str, text: str) -> float:
    """Quick Jaccard token overlap for the blend."""
    q = set(_TOK_RE.findall(query.lower()))
    t = set(_TOK_RE.findall(text.lower()))
    if not q or not t:
        return 0.0
    return len(q & t) / len(q | t)
