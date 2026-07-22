"""Tests for P5 OllamaParser — LLM-based intent classification.

Tests verify:
  - Fallback to RuleBasedParser when Ollama is unreachable.
  - Correct parsing of well-formed LLM JSON responses.
  - Graceful handling of malformed/empty responses.
  - Unknown intent triggers fallback.
  - The parser implements the Parser Protocol (parse method signature).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from compy.daemon.models import ParsedQuery
from compy.daemon.ollama_parser import OllamaParser, _INTENTS
from compy.daemon.parser import RuleBasedParser


def _mock_ollama_response(payload: dict) -> bytes:
    """Build a fake urlopen response object returning the given JSON payload."""
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    mock.read = MagicMock(return_value=json.dumps(payload).encode("utf-8"))
    return mock


def test_ollama_parser_implements_protocol():
    """OllamaParser must have parse(question, selection_text) -> ParsedQuery."""
    p = OllamaParser()
    assert hasattr(p, "parse")
    # Verify it can be called with the Protocol signature.
    result = p.parse("test", None)
    assert isinstance(result, ParsedQuery)


def test_fallback_when_ollama_unreachable():
    """When Ollama server is down, must fall back to RuleBasedParser."""
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        p = OllamaParser(timeout_s=0.5)
        result = p.parse("where is authenticate defined?", None)
    # Should have fallen back to rule-based — definition intent.
    # Note: RuleBasedParser extracts symbol from selection_text, not from
    # the question. Without a selection, symbol is None.
    assert result.intent == "definition"


def test_fallback_on_timeout():
    """Timeout should trigger fallback, not crash."""
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
        p = OllamaParser(timeout_s=0.5)
        result = p.parse("what calls handle_request", None)
    # Fallback: "what calls" → relational.
    assert result.intent == "relational"


def test_correct_parse_from_valid_json():
    """Well-formed LLM JSON response should produce correct ParsedQuery."""
    llm_response = {
        "response": json.dumps({
            "intent": "definition",
            "symbol": "authenticate",
            "keywords": ["authenticate", "definition"],
            "confidence": 0.9,
        })
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)):
        p = OllamaParser()
        result = p.parse("where is authenticate defined?", None)
    assert result.intent == "definition"
    assert result.symbol == "authenticate"
    assert "authenticate" in result.keywords
    assert result.confidence == 0.9


def test_malformed_json_triggers_fallback():
    """Garbage response should fall back, not crash."""
    llm_response = {"response": "This is not JSON at all."}
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)):
        p = OllamaParser()
        result = p.parse("where is foo defined?", None)
    # Fallback to rule-based.
    assert result.intent == "definition"


def test_empty_response_triggers_fallback():
    """Empty response string should fall back."""
    llm_response = {"response": ""}
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)):
        p = OllamaParser()
        result = p.parse("explain handle_request", None)
    # Fallback: explain intent.
    assert result.intent == "explain"


def test_unknown_intent_triggers_fallback():
    """If the model emits an intent not in the allowed set, fall back."""
    llm_response = {
        "response": json.dumps({
            "intent": "autocomplete",
            "symbol": "foo",
            "keywords": ["foo"],
            "confidence": 0.5,
        })
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)):
        p = OllamaParser()
        result = p.parse("where is foo?", None)
    # Fallback: "where is" → fuzzy.
    assert result.intent == "fuzzy"


def test_null_symbol_handled():
    """LLM returning 'null' for symbol should produce symbol=None."""
    llm_response = {
        "response": json.dumps({
            "intent": "overview",
            "symbol": "null",
            "keywords": ["architecture", "modules"],
            "confidence": 0.7,
        })
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)):
        p = OllamaParser()
        result = p.parse("how does this codebase work?", None)
    assert result.intent == "overview"
    assert result.symbol is None


def test_keywords_truncated_to_8():
    """Keywords list should be capped at 8 entries."""
    llm_response = {
        "response": json.dumps({
            "intent": "fuzzy",
            "symbol": None,
            "keywords": [f"word{i}" for i in range(20)],
            "confidence": 0.5,
        })
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)):
        p = OllamaParser()
        result = p.parse("some vague query", None)
    assert len(result.keywords) <= 8


def test_confidence_clamped():
    """Confidence outside 0-1 should be clamped."""
    llm_response = {
        "response": json.dumps({
            "intent": "fuzzy",
            "symbol": None,
            "keywords": ["test"],
            "confidence": 5.0,
        })
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)):
        p = OllamaParser()
        result = p.parse("test query", None)
    assert result.confidence == 1.0


def test_all_intents_are_valid_strings():
    """The _INTENTS tuple should contain non-empty strings."""
    for intent in _INTENTS:
        assert isinstance(intent, str)
        assert len(intent) > 0


def test_rename_symbol_format():
    """Rename intent should produce 'old::new' symbol format from the LLM."""
    llm_response = {
        "response": json.dumps({
            "intent": "rename",
            "symbol": "old_func::new_func",
            "keywords": ["rename", "old_func", "new_func"],
            "confidence": 0.95,
        })
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)):
        p = OllamaParser()
        result = p.parse("rename old_func to new_func", None)
    assert result.intent == "rename"
    assert result.symbol == "old_func::new_func"


def test_parse_with_selection_text():
    """Selection text should be included in the prompt sent to Ollama."""
    llm_response = {
        "response": json.dumps({
            "intent": "explain",
            "symbol": "handle_request",
            "keywords": ["handle_request"],
            "confidence": 0.85,
        })
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)) as mock_urlopen:
        p = OllamaParser()
        result = p.parse("what does this function do?", "def handle_request(req):")
    assert result.intent == "explain"
    assert result.symbol == "handle_request"
    # Verify the prompt included the selection text.
    call_body = json.loads(mock_urlopen.call_args[0][0].data)
    assert "handle_request" in call_body["prompt"]


def test_trace_intent_from_stack_trace():
    """Stack trace input should classify as trace."""
    llm_response = {
        "response": json.dumps({
            "intent": "trace",
            "symbol": None,
            "keywords": ["traceback", "error"],
            "confidence": 0.95,
        })
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(llm_response)):
        p = OllamaParser()
        result = p.parse('Traceback (most recent call last):\n  File "app.py", line 42', None)
    assert result.intent == "trace"
