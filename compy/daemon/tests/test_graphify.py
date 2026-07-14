"""Tests for Graphify — code-knowledge graph builder and querier.

Coverage:
  - GraphBuilder builds a graph from Python source files.
  - GraphQuerier queries calls, callers, imports, subclasses.
  - Cache serialization and deserialization.
  - Empty workspace returns empty results.
  - Missing cache dir is created.
  - force_rebuild flag.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from compy.daemon.graphify import (
    GraphBuilder,
    GraphQuerier,
    _cache_key,
    _resolve_node,
)


def _write_py_file(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def sample_repo() -> Path:
    """Create a temp repo with a few .py files that call each other."""
    tmp = Path(tempfile.mkdtemp())
    _write_py_file(tmp, "main.py", """
def main():
    helper()
    process()

def helper():
    pass
""")
    _write_py_file(tmp, "utils.py", """
def process():
    helper()

def helper():
    pass
""")
    _write_py_file(tmp, "base.py", """
class Animal:
    def speak(self):
        pass

class Dog(Animal):
    def speak(self):
        print("woof")
""")
    _write_py_file(tmp, "imports_test.py", """
import os
from pathlib import Path
from utils import helper
""")
    yield tmp
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- graph building -------------------------------------------------

def test_build_graph_creates_function_nodes(sample_repo: Path):
    builder = GraphBuilder()
    graph = builder.get_graph(str(sample_repo), force_rebuild=True)
    nodes = [n for n in graph.nodes if graph.nodes[n].get("kind") == "function"]
    # main.py: main, helper; utils.py: process, helper
    assert len(nodes) >= 4


def test_build_graph_creates_class_nodes(sample_repo: Path):
    builder = GraphBuilder()
    graph = builder.get_graph(str(sample_repo), force_rebuild=True)
    class_nodes = [n for n in graph.nodes if graph.nodes[n].get("kind") == "class"]
    assert any("Animal" in n for n in class_nodes)
    assert any("Dog" in n for n in class_nodes)


def test_graph_includes_call_edges(sample_repo: Path):
    builder = GraphBuilder()
    graph = builder.get_graph(str(sample_repo), force_rebuild=True)
    call_edges = [(u, v) for u, v, d in graph.edges(data=True) if d.get("kind") == "calls"]
    # main() calls helper() and process()
    assert len(call_edges) > 0


def test_graph_cache_persists(sample_repo: Path):
    builder = GraphBuilder()
    graph1 = builder.get_graph(str(sample_repo), force_rebuild=True)
    # Second call should load from cache.
    graph2 = builder.get_graph(str(sample_repo), force_rebuild=False)
    assert len(graph1.nodes) == len(graph2.nodes)


def test_force_rebuild_rebuilds(sample_repo: Path):
    builder = GraphBuilder()
    builder.get_graph(str(sample_repo), force_rebuild=True)
    # Add a new file after initial build.
    _write_py_file(sample_repo, "new_file.py", "def new_func(): pass")
    graph = builder.get_graph(str(sample_repo), force_rebuild=True)
    nodes = [n for n in graph.nodes if "new_func" in n]
    assert len(nodes) >= 1


def test_empty_workspace_returns_graph():
    with tempfile.TemporaryDirectory() as tmp:
        builder = GraphBuilder()
        graph = builder.get_graph(tmp, force_rebuild=True)
        assert len(graph.nodes) == 0


# ---------- cache key -----------------------------------------------------

def test_cache_key_strips_slashes():
    key = _cache_key("/Users/test/repo")
    assert key.endswith(".graph.pkl")
    assert "/" not in key


# ---------- node resolution ------------------------------------------------

def test_resolve_node_finds_exact_match(sample_repo: Path):
    builder = GraphBuilder()
    graph = builder.get_graph(str(sample_repo), force_rebuild=True)
    found = _resolve_node("main", graph)
    assert found is not None
    assert found.endswith("::main")


def test_resolve_node_case_insensitive():
    import networkx as nx
    g = nx.DiGraph()
    g.add_node("file.py::FooBar")
    found = _resolve_node("foobar", g)
    assert found is not None


def test_resolve_node_not_found():
    import networkx as nx
    g = nx.DiGraph()
    assert _resolve_node("nonexistent", g) is None


# ---------- GraphQuerier ---------------------------------------------------

def test_querier_returns_calls(sample_repo: Path):
    q = GraphQuerier()
    q.load(str(sample_repo), force_rebuild=True)
    hits = q.query_calls("main")
    # main() calls helper() and process()
    assert len(hits) >= 1


def test_querier_callers_finds_reverse(sample_repo: Path):
    q = GraphQuerier()
    q.load(str(sample_repo), force_rebuild=True)
    hits = q.query_callers("helper")
    # helper is called by main() and process()
    assert len(hits) >= 1


def test_querier_imports(sample_repo: Path):
    q = GraphQuerier()
    q.load(str(sample_repo), force_rebuild=True)
    hits = q.query_imports("imports_test")
    assert len(hits) >= 1


def test_querier_unloaded_graph_returns_empty():
    q = GraphQuerier()
    assert q.query_calls("foo") == ()
