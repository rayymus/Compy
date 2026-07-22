"""Tests for _extract_symbol_from_question — symbol extraction from questions.

Verifies that relational/blast_radius/explain queries without a selection
still get a symbol extracted from the question text. This is the fix for
"who calls handle_request" having symbol=None, which broke graph queries.
"""

from __future__ import annotations

from compy.daemon.parser import _extract_symbol_from_question


def test_snake_case_symbol_from_who_calls():
    """'who calls handle_request' → 'handle_request'."""
    assert _extract_symbol_from_question("who calls handle_request") == "handle_request"


def test_snake_case_symbol_from_what_calls():
    """'what calls validate_token' → 'validate_token'."""
    assert _extract_symbol_from_question("what calls validate_token") == "validate_token"


def test_symbol_from_what_does_call():
    """'what does authenticate call' → 'authenticate'."""
    assert _extract_symbol_from_question("what does authenticate call") == "authenticate"


def test_symbol_from_blast_radius():
    """'what breaks if I change validate_token' → 'validate_token'."""
    result = _extract_symbol_from_question("what breaks if I change validate_token")
    assert result == "validate_token"


def test_symbol_from_explain():
    """'explain handle_request' → 'handle_request'."""
    assert _extract_symbol_from_question("explain handle_request") == "handle_request"


def test_camel_case_symbol():
    """'who calls sendSelectionEnvelope' → 'sendSelectionEnvelope'."""
    result = _extract_symbol_from_question("who calls sendSelectionEnvelope")
    assert result == "sendSelectionEnvelope"


def test_no_symbol_when_only_query_words():
    """'who calls it' → None (no real symbol)."""
    assert _extract_symbol_from_question("who calls it") is None


def test_no_symbol_when_empty():
    """Empty string → None."""
    assert _extract_symbol_from_question("") is None


def test_prefers_snake_case_over_camel():
    """When both present, snake_case wins (Python convention)."""
    result = _extract_symbol_from_question("who calls my_func and MyClass")
    assert result == "my_func"


def test_filters_query_intent_words():
    """Query words like 'calls', 'who', 'what' are filtered out."""
    result = _extract_symbol_from_question("who calls handle_request in auth module")
    assert result == "handle_request"


def test_symbol_from_where_used():
    """'where is handle_request used' → 'handle_request'."""
    result = _extract_symbol_from_question("where is handle_request used")
    assert result == "handle_request"


def test_longest_snake_case_wins():
    """When multiple snake_case tokens, longest wins (more specific)."""
    result = _extract_symbol_from_question("who calls short_name and very_long_function_name")
    assert result == "very_long_function_name"


# ── Integration: parser end-to-end with symbol extraction ──

def test_parser_relational_extracts_symbol_without_selection():
    """Full parser: 'who calls handle_request' without selection → symbol extracted."""
    from compy.daemon.parser import RuleBasedParser
    p = RuleBasedParser()
    result = p.parse("who calls handle_request", None)
    assert result.intent == "relational"
    assert result.symbol == "handle_request"
    assert result.confidence >= 0.75


def test_parser_blast_radius_extracts_symbol_without_selection():
    """Full parser: 'what breaks if I change validate_token' → blast_radius with symbol."""
    from compy.daemon.parser import RuleBasedParser
    p = RuleBasedParser()
    result = p.parse("what breaks if I change validate_token", None)
    assert result.intent == "blast_radius"
    assert result.symbol == "validate_token"


def test_parser_explain_extracts_symbol_without_selection():
    """Full parser: 'explain handle_request' → explain with symbol."""
    from compy.daemon.parser import RuleBasedParser
    p = RuleBasedParser()
    result = p.parse("explain handle_request", None)
    assert result.intent == "explain"
    assert result.symbol == "handle_request"
