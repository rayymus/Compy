"""Tests for the rule-based parser.

Coverage:
  - Intent classification for the three regex rule buckets + the fallback fuzzy.
  - Symbol extraction from selection (snake_case preferred, longest-tok fallback).
  - Confidence boost when both selection and extractable symbol are present.
  - Edge cases: empty question, empty selection, ambiguous wording.
"""

from __future__ import annotations

from compy.daemon.models import Selection
from compy.daemon.parser import RuleBasedParser


def _parse(question: str, selection: str | None = None):
    parser = RuleBasedParser()
    sel = Selection(text=selection) if selection else None
    return parser.parse(question, sel.text if sel else None)


def test_references_intent_from_phrase():
    parsed = _parse("where else is get_ability used?", selection="def get_ability(self): pass")
    assert parsed.intent == "references"
    assert parsed.symbol == "get_ability"
    assert parsed.confidence >= 0.85


def test_definition_intent_from_phrase():
    parsed = _parse("where is parse_request defined?", selection="def parse_request():")
    assert parsed.intent == "definition"
    assert parsed.symbol == "parse_request"


def test_fuzzy_intent_when_no_structural_phrase():
    parsed = _parse("what does the parser do?", selection="def parse(): pass")
    assert parsed.intent == "fuzzy"
    # Symbol is now extracted from selection regardless of intent — the parser exposes
    # the extraction and the orchestrator routes by intent at a higher level.
    assert parsed.symbol == "parse"
    assert "parser" in parsed.keywords


def test_fallback_intent_when_no_rule_matches():
    parsed = _parse("huh?")
    assert parsed.intent == "fuzzy"
    assert parsed.confidence < 0.6


def test_selection_symbol_bonus_raises_confidence():
    with_sel = _parse("where else is X used?", selection="X_value_here()")
    bare = _parse("where else is X used?")
    assert with_sel.confidence > bare.confidence


def test_snake_case_symbol_preferred():
    parsed = _parse("anything", selection="class Foo: def method_x(self): return bar")
    assert parsed.symbol == "method_x"


def test_keywords_are_dedup_and_capped():
    parsed = _parse("find files files files in the repo repo")
    assert list(parsed.keywords).count("files") == 1
    assert len(parsed.keywords) <= 8


def test_empty_question_low_confidence_fuzzy():
    parsed = _parse("   ")
    assert parsed.confidence < 0.6
    assert parsed.intent == "fuzzy"


# ---------- new intents: history + relational -------------------------------

def test_history_intent_why_was():
    parsed = _parse("why was get_ability changed", selection="def get_ability(self): pass")
    assert parsed.intent == "history"


def test_history_intent_who_added():
    parsed = _parse("who added the authenticate function")
    assert parsed.intent == "history"


def test_history_intent_git_blame():
    parsed = _parse("git blame this line", selection="def foo(): pass")
    assert parsed.intent == "history"


def test_relational_intent_calls():
    parsed = _parse("what calls get_ability", selection="def get_ability(self): pass")
    assert parsed.intent == "relational"


def test_relational_intent_callers():
    parsed = _parse("callers of helper")
    assert parsed.intent == "relational"


def test_relational_intent_imports():
    parsed = _parse("what imports os")
    assert parsed.intent == "relational"


def test_relational_intent_inherits():
    parsed = _parse("what inherits from Animal")
    assert parsed.intent == "relational"


def test_references_still_works_after_new_rules():
    """Regression: existing intent rules must still match after adding new ones."""
    parsed = _parse("where else is get_ability used", selection="def get_ability(self): pass")
    assert parsed.intent == "references"
