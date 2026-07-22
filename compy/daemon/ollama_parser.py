"""P5: LLM-based intent parser using a local Ollama model.

Replaces the 18-rule regex parser with a tiny local model that classifies
intent, extracts a symbol, and suggests keywords — all in one Ollama call.

Falls back to RuleBasedParser on any failure (server down, bad JSON, timeout).
The orchestrator depends only on the Parser Protocol, so swapping is transparent.

The prompt is compact (~300 tokens) and asks for strict JSON output so parsing
is a single json.loads() — no fragile regex extraction from prose.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .interfaces import Parser
from .models import ParsedQuery
from .parser import RuleBasedParser, _log_parse_decision

# All intents the model can emit — must match orchestrator routing.
_INTENTS = (
    "trace", "explain", "graph_path", "history", "rationale",
    "relational", "blast_radius", "references", "definition",
    "convention", "dedup", "dead_code", "overview", "rename",
    "format", "extract_variable", "add_type_hints", "fuzzy",
)

_SYSTEM_PROMPT = (
    "You are a code-search intent classifier. Given a developer's question "
    "and optional code selection, classify the intent and extract key terms.\n\n"
    "Respond with STRICT JSON, no prose, no markdown fences:\n"
    '{"intent": "<one of the intents>", "symbol": "<identifier or null>", '
    '"keywords": ["<word>", ...], "confidence": <0.0-1.0>}\n\n'
    "Intents: " + ", ".join(_INTENTS) + "\n\n"
    "Rules:\n"
    "- symbol: the code identifier the user is asking about (snake_case or CamelCase). "
    "null if the question is general.\n"
    "- keywords: 1-6 lowercase search terms from the question, excluding stop words "
    "(the, is, where, what, def, function, class, return, import).\n"
    "- confidence: how sure you are of the intent. 0.9+ for explicit structural queries "
    "('where is X defined', 'what calls Y'). 0.5-0.7 for fuzzy searches. "
    "0.3-0.4 for very vague questions.\n"
    "- For 'rename X to Y', symbol is 'X::Y'.\n"
    "- For 'how are X and Y connected', symbol is 'X::Y'.\n"
    "- If the question contains a stack trace, intent is 'trace'.\n"
    "- If unsure, use 'fuzzy' with the extracted keywords."
)


class OllamaParser:
    """LLM-powered parser. Ollama generate endpoint, strict JSON output.

    Implements the Parser Protocol. On any failure (server down, timeout,
    malformed JSON, unknown intent), falls back to RuleBasedParser so the
    pipeline never breaks.
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str = "http://localhost:11434",
        timeout_s: float = 8.0,
        fallback: Parser | None = None,
    ) -> None:
        self._model = model or os.environ.get(
            "COMPY_OLLAMA_MODEL", "qwen2.5-coder:1.5b"
        )
        self._url = f"{base_url}/api/generate"
        self._timeout = timeout_s
        self._fallback = fallback or RuleBasedParser()

    def parse(self, question: str, selection_text: str | None) -> ParsedQuery:
        result = self._try_ollama(question, selection_text)
        if result is not None:
            _log_parse_decision(question, bool(selection_text), result)
            return result
        # Fallback: regex parser always works.
        return self._fallback.parse(question, selection_text)

    def _try_ollama(
        self, question: str, selection_text: str | None
    ) -> ParsedQuery | None:
        """Attempt Ollama classification. Returns None on any failure."""
        user_prompt = self._build_prompt(question, selection_text)
        body = json.dumps({
            "model": self._model,
            "system": _SYSTEM_PROMPT,
            "prompt": user_prompt,
            "stream": False,
            "format": "json",  # Ollama native JSON mode — forces valid JSON.
            "options": {"temperature": 0.1, "num_predict": 128},
        }).encode("utf-8")

        req = urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError):
            return None

        raw = payload.get("response", "").strip()
        if not raw:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None

        intent = data.get("intent", "").strip().lower()
        if intent not in _INTENTS:
            return None

        symbol = data.get("symbol")
        if isinstance(symbol, str) and symbol.lower() in ("null", "none", ""):
            symbol = None

        keywords_raw = data.get("keywords", [])
        if not isinstance(keywords_raw, list):
            keywords_raw = []
        keywords = tuple(
            str(k).lower().strip()
            for k in keywords_raw
            if isinstance(k, str) and len(k.strip()) >= 2
        )[:8]

        confidence = data.get("confidence", 0.5)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        return ParsedQuery(
            intent=intent,
            symbol=symbol if isinstance(symbol, str) else None,
            keywords=keywords,
            confidence=round(confidence, 2),
        )

    @staticmethod
    def _build_prompt(question: str, selection_text: str | None) -> str:
        parts = [f'Question: "{question}"']
        if selection_text:
            # Trim to 200 chars — the model just needs to see the symbol shape.
            sel = selection_text.strip()[:200]
            parts.append(f'Selected code: "{sel}"')
        else:
            parts.append("Selected code: (none)")
        parts.append("\nClassify:")
        return "\n".join(parts)
