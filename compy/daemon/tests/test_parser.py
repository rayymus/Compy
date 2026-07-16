"""Tests for the rule-based parser.

Coverage:
  - Intent classification for the three regex rule buckets + the fallback fuzzy.
  - Symbol extraction from selection (snake_case preferred, longest-tok fallback).
  - Confidence boost when both selection and extractable symbol are present.
  - Edge cases: empty question, empty selection, ambiguous wording.
  - New intents: trace, rationale, blast_radius, convention.
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
    assert list(parsed.keywords).count("file") == 1  # Stemmed: files → file
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


# ---------- trace intent ----------------------------------------------------

def test_trace_intent_python_traceback():
    parsed = _parse(
        'Traceback (most recent call last):\n  File "src/app.py", line 42, in handler\n    result = process()\n  File "src/utils.py", line 17, in process\n    raise ValueError("oops")'
    )
    assert parsed.intent == "trace"


def test_trace_intent_file_line_pattern():
    parsed = _parse('File "src/main.py", line 100, in run')
    assert parsed.intent == "trace"


def test_trace_intent_at_pattern():
    parsed = _parse("at /src/app.js:15:8")
    assert parsed.intent == "trace"


# ---------- rationale intent ------------------------------------------------

def test_rationale_why_does_this_exist():
    parsed = _parse("why does this null check exist")
    assert parsed.intent == "rationale"


def test_rationale_explain_this():
    parsed = _parse("explain this workaround")
    assert parsed.intent == "rationale"


def test_rationale_reason_for():
    parsed = _parse("reason for the fallback logic")
    assert parsed.intent == "rationale"


# ---------- blast_radius intent ---------------------------------------------

def test_blast_radius_what_breaks():
    parsed = _parse("what breaks if I change get_ability")
    assert parsed.intent == "blast_radius"


def test_blast_radius_what_depends():
    parsed = _parse("what depends on authenticate")
    assert parsed.intent == "blast_radius"


def test_blast_radius_impact_of():
    parsed = _parse("impact of changing the config parser")
    assert parsed.intent == "blast_radius"


# ---------- convention intent -----------------------------------------------

def test_convention_how_do_we():
    parsed = _parse("how do we handle errors here")
    assert parsed.intent == "convention"


def test_convention_whats_the_pattern():
    parsed = _parse("what's the pattern for logging")
    assert parsed.intent == "convention"


def test_convention_show_me_examples():
    parsed = _parse("show me examples of middleware setup")
    assert parsed.intent == "convention"


def test_convention_typically():
    parsed = _parse("how is authentication typically done")
    assert parsed.intent == "convention"


# ---------- dedup intent -----------------------------------------------------

def test_dedup_does_this_exist():
    parsed = _parse("does this already exist")
    assert parsed.intent == "dedup"


def test_dedup_duplicate_of():
    parsed = _parse("duplicate of the auth handler")
    assert parsed.intent == "dedup"


def test_dedup_anyone_wrote():
    parsed = _parse("anyone already wrote a rate limiter")
    assert parsed.intent == "dedup"


# ---------- CamelCase keyword splitting --------------------------------------

def test_camelcase_split_into_keywords():
    """CamelCase words should split into constituent parts for fuzzy search.

    Without splitting, "handleRequest" lowercases to "handlerequest" — a
    single token that never matches anything in a real codebase. Splitting
    gives ripgrep "handle" and "request" as separate greppable keywords.
    """
    parsed = _parse("where is handleRequest")
    assert "handle" in parsed.keywords
    assert "request" in parsed.keywords
    assert "handlerequest" not in parsed.keywords


def test_pascalcase_split_into_keywords():
    """PascalCase (leading uppercase) should also split."""
    parsed = _parse("where is HTTPServer")
    assert "http" in parsed.keywords
    assert "server" in parsed.keywords


def test_camelcase_preserves_multi_word_phrases():
    """After splitting, multi-word phrases are still built from the split parts.

    Uses "what does ... actually do" (fuzzy intent) — "where is ... defined"
    would match the definition intent, which skips keyword extraction.
    """
    parsed = _parse("what does handleRequest actually do")
    # "handle request" should appear as a phrase
    assert any("handle" in kw and "request" in kw for kw in parsed.keywords)


def test_camelcase_mixed_acronyms():
    """Mixed case with acronyms: MyAPIKey → my, api, key."""
    parsed = _parse("where is MyAPIKey")
    assert "api" in parsed.keywords
    assert "key" in parsed.keywords


# ---------- Session 20: selection-aware search fixes ------------------------

def test_deictic_this_used_with_selection_promotes_to_references():
    """'Where is this used' + selected symbol → references, not fuzzy.

    Gap 1: The selection IS the thing the user is asking about.  'this'
    with a selection means 'find references to what I selected.'
    """
    parsed = _parse(
        "where is this database table used",
        selection="users = Table('users', ...)",
    )
    assert parsed.intent == "references"
    assert parsed.symbol == "users"
    assert parsed.confidence >= 0.85


def test_deictic_that_used_without_selection_stays_fuzzy():
    """Without a selection, 'that' can't be grounded — stays fuzzy."""
    parsed = _parse("where is that database table used")
    assert parsed.intent == "fuzzy"


def test_selection_symbol_injected_into_fuzzy_keywords():
    """Gap 2: When fuzzy with a selected symbol, the symbol should be the
    first keyword so it gets searched first."""
    parsed = _parse(
        "what does this do exactly",
        selection="def authenticate_user(token): pass",
    )
    assert parsed.intent == "fuzzy"
    assert parsed.symbol == "authenticate_user"
    # Symbol should be injected as the first keyword.
    assert parsed.keywords[0] == "authenticate_user"


def test_used_no_longer_a_stopword():
    """Gap 3: 'used' was a stopword — it's now preserved as a keyword."""
    parsed = _parse("where is this function used")
    assert "used" in parsed.keywords


def test_uses_no_longer_a_stopword():
    """'uses' should survive keyword extraction."""
    parsed = _parse("who uses this class")
    assert "uses" in parsed.keywords


def test_existing_references_still_works():
    """Regression: existing 'where else' references must still fire."""
    parsed = _parse(
        "where else is get_ability used",
        selection="def get_ability(self): pass",
    )
    assert parsed.intent == "references"
    assert parsed.symbol == "get_ability"


def test_deictic_it_called_promotes_to_references():
    """'Where is it called' + selection → references."""
    parsed = _parse(
        "where is it called from",
        selection="def helper(): pass",
    )
    assert parsed.intent == "references"
    assert parsed.symbol == "helper"
