"""Graphify — persistent code-knowledge graph for relational queries.

Uses tree-sitter (Python grammar) to parse source files and build a NetworkX DiGraph
of symbols (functions, classes, methods) connected by edges (calls, imports, inheritance).
The graph is serialized to disk (~/.compy/graph.pkl) for persistence across sessions.

Query methods:
  - query_calls(symbol)      → what does this function/class call?
  - query_callers(symbol)    → what calls this function/class?
  - query_imports(symbol)    → what does this module/function import?
  - query_subclasses(symbol) → what inherits from this class?

All query methods return tuples of GrepHit so they plug into the existing reasoner chain
without any structural changes.
"""

from __future__ import annotations

import pickle
import re
import time
from pathlib import Path
from typing import Any

import networkx as nx

from .interfaces import ReasonerUnavailable
from .models import GrepHit

# Lazy imports — tree-sitter + tree-sitter-python are only imported when _build_graph()
# is actually called. This prevents import-time crashes when the versions are mismatched
# (e.g. tree-sitter 0.26.0 + tree-sitter-python 0.25.0 on conda Python). The error
# surfaces only when graphify is actually used, with a clear message.
_lazy_imports_done = False
_tspython = None
_Language = None
_Node = None
_Parser = None
_Query = None
_QueryCursor = None


def _ensure_imports() -> None:
    global _lazy_imports_done, _tspython, _Language, _Node, _Parser, _Query, _QueryCursor
    if _lazy_imports_done:
        return
    try:
        import tree_sitter_python as _tspython
        from tree_sitter import Language as _Language
        from tree_sitter import Parser as _Parser
        from tree_sitter import Query as _Query
        from tree_sitter import QueryCursor as _QueryCursor
    except ImportError as exc:
        raise ReasonerUnavailable(
            f"Graphify requires tree-sitter and tree-sitter-python: {exc}. "
            f"Install with: pip install tree-sitter tree-sitter-python"
        ) from exc
    _lazy_imports_done = True

# Cache path — one graph per workspace root.
CACHE_DIR = Path.home() / ".compy"
GRAPH_EXT = ".graph.pkl"

# tree-sitter query for function/class definitions.
_DEF_QUERY_STR = """
(function_definition
  name: (identifier) @func.name
  body: (block) @func.body) @func.def

(class_definition
  name: (identifier) @class.name
  body: (block) @class.body) @class.def
"""

# tree-sitter query for call expressions (function calls).
_CALL_QUERY_STR = """
(call
  function: (identifier) @call.name) @call.expr

(call
  function: (attribute
    object: (identifier) @call.obj
    attribute: (identifier) @call.attr)) @call.expr
"""

# tree-sitter query for import statements.
_IMPORT_QUERY_STR = """
(import_statement
  name: (dotted_name) @import.name) @import.stmt

(import_from_statement
  module_name: (dotted_name) @import.from
  name: (dotted_name) @import.name) @import.stmt
"""

# tree-sitter query for class inheritance.
_INHERIT_QUERY_STR = """
(class_definition
  name: (identifier) @class.name
  superclasses: (argument_list
    (identifier) @parent.name)) @class.def
"""


def _build_graph(workspace_root: str) -> nx.DiGraph:
    """Build a NetworkX DiGraph from all .py files in workspace_root."""
    _ensure_imports()
    lang = _Language(_tspython.language())  # type: ignore[arg-type]
    parser = _Parser(lang)

    graph = nx.DiGraph()

    def_q = _Query(lang, _DEF_QUERY_STR)
    call_q = _Query(lang, _CALL_QUERY_STR)
    import_q = _Query(lang, _IMPORT_QUERY_STR)
    inherit_q = _Query(lang, _INHERIT_QUERY_STR)

    py_files = list(Path(workspace_root).rglob("*.py"))
    # Skip hidden dirs and test caches.
    py_files = [
        f for f in py_files
        if not any(p.startswith(".") for p in f.parts)
        and "__pycache__" not in str(f)
    ]

    for file_path in py_files:
        try:
            source = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        tree = parser.parse(source.encode("utf-8"))
        rel_path = str(file_path.relative_to(workspace_root))

        # --- definitions (functions + classes) ---
        def_cur = _QueryCursor(def_q)
        for cap_name, cap_nodes in def_cur.captures(tree.root_node).items():
            for cap_node in cap_nodes:
                if cap_name == "func.name":
                    node_text = cap_node.text.decode("utf-8")
                    node_id = f"{rel_path}::{node_text}"
                    graph.add_node(node_id, kind="function", file=rel_path,
                                   line=cap_node.start_point[0] + 1,
                                   snippet=_node_snippet(cap_node, source))
                elif cap_name == "class.name":
                    node_text = cap_node.text.decode("utf-8")
                    node_id = f"{rel_path}::{node_text}"
                    graph.add_node(node_id, kind="class", file=rel_path,
                                   line=cap_node.start_point[0] + 1,
                                   snippet=_node_snippet(cap_node, source))

        # --- calls ---
        call_cur = _QueryCursor(call_q)
        for cap_name, cap_nodes in call_cur.captures(tree.root_node).items():
            for cap_node in cap_nodes:
                if cap_name == "call.expr":
                    caller = _enclosing_symbol(cap_node, tree.root_node, source, rel_path, graph)
                    if caller is None:
                        continue
                    callee_name = _extract_call_name(cap_node, source)
                    if callee_name is None:
                        continue
                    callee_id = _resolve_callee(callee_name, rel_path, graph)
                    if callee_id:
                        graph.add_edge(caller, callee_id, kind="calls",
                                       line=cap_node.start_point[0] + 1)
                    else:
                        unresolved = f"<unknown>::{callee_name}"
                        if not graph.has_node(unresolved):
                            graph.add_node(unresolved, kind="unknown", file="<unknown>", line=0,
                                           snippet=f"unresolved: {callee_name}")
                        graph.add_edge(caller, unresolved, kind="calls",
                                       line=cap_node.start_point[0] + 1)

        # --- imports ---
        import_cur = _QueryCursor(import_q)
        for cap_name, cap_nodes in import_cur.captures(tree.root_node).items():
            for cap_node in cap_nodes:
                if cap_name == "import.stmt":
                    enclosing = _enclosing_module(tree.root_node, source, rel_path, graph)
                    if enclosing is None:
                        continue
                    imported = cap_node.text.decode("utf-8")
                    graph.add_edge(enclosing, f"<import>::{imported}", kind="imports",
                                   line=cap_node.start_point[0] + 1, label=imported)

        # --- inheritance ---
        inherit_cur = _QueryCursor(inherit_q)
        for cap_name, cap_nodes in inherit_cur.captures(tree.root_node).items():
            for cap_node in cap_nodes:
                if cap_name == "parent.name":
                    parent_name = cap_node.text.decode("utf-8")
                    # Find the class that has this parent.
                    enclosing = _enclosing_symbol(cap_node, tree.root_node, source, rel_path, graph)
                    if enclosing:
                        parent_id = _resolve_callee(parent_name, rel_path, graph)
                        if parent_id:
                            graph.add_edge(enclosing, parent_id, kind="inherits",
                                           line=cap_node.start_point[0] + 1)

    return graph


def _node_snippet(node: Any, source: str) -> str:
    """Extract the first line of a node as a snippet."""
    start = node.start_point[0]
    lines = source.splitlines()
    if start < len(lines):
        return lines[start][:200]
    return node.text.decode("utf-8")[:200]


def _enclosing_symbol(node: Any, root: Any, source: str, rel_path: str,
                      graph: nx.DiGraph) -> str | None:
    """Walk up the tree to find the enclosing function or class."""
    current: Any | None = node
    while current is not None:
        if current.type in ("function_definition", "class_definition"):
            # Find the name child.
            for child in current.children:
                if child.type == "identifier":
                    name = child.text.decode("utf-8")
                    return f"{rel_path}::{name}"
            break
        current = current.parent
    # Fall back to module-level.
    module_id = f"{rel_path}::<module>"
    if not graph.has_node(module_id):
        graph.add_node(module_id, kind="module", file=rel_path, line=1,
                       snippet=f"module {rel_path}")
    return module_id


def _enclosing_module(root: Any, source: str, rel_path: str,
                      graph: nx.DiGraph) -> str | None:
    """Get the module-level node for a file."""
    module_id = f"{rel_path}::<module>"
    if not graph.has_node(module_id):
        graph.add_node(module_id, kind="module", file=rel_path, line=1,
                       snippet=f"module {rel_path}")
    return module_id


def _extract_call_name(node: Any, source: str) -> str | None:
    """Extract the callee name from a call expression."""
    for child in node.children:
        if child.type == "identifier":
            return child.text.decode("utf-8")
        elif child.type == "attribute":
            # obj.method() — return method name
            ids = [c.text.decode("utf-8") for c in child.children if c.type == "identifier"]
            if ids:
                return ids[-1]
    return None


def _resolve_callee(name: str, current_file: str, graph: nx.DiGraph) -> str | None:
    """Find a function/class node in the graph matching the name."""
    # First, look in the same file.
    prefix = f"{current_file}::"
    for node_id in graph.nodes:
        if node_id.startswith(prefix) and node_id.endswith(f"::{name}"):
            return node_id
    # Then, look across all files.
    for node_id in graph.nodes:
        if node_id.endswith(f"::{name}") and graph.nodes[node_id].get("kind") in ("function", "class"):
            return node_id
    return None


class GraphBuilder:
    """Builds and caches a code-knowledge graph for a workspace."""

    # Number of seconds to cache the staleness check result.
    # Avoids walking the entire workspace tree on every query.
    _staleness_cache_ttl: float = 5.0

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir or CACHE_DIR
        # Per-workspace staleness cache: (checked_at_timestamp, is_stale)
        self._staleness: dict[str, tuple[float, bool]] = {}

    def _is_stale(self, workspace_root: str, cache_path: Path) -> bool:
        """Check if any .py file in the workspace is newer than the cached graph.

        Result is cached for _staleness_cache_ttl seconds so repeated queries
        don't walk the full tree every time.
        """
        now = time.time()
        cached = self._staleness.get(workspace_root)
        if cached is not None:
            checked_at, was_stale = cached
            if now - checked_at < self._staleness_cache_ttl:
                return was_stale

        try:
            cache_mtime = cache_path.stat().st_mtime
        except OSError:
            self._staleness[workspace_root] = (now, True)
            return True  # No cache → stale by definition.

        ws_path = Path(workspace_root)
        if not ws_path.is_dir():
            self._staleness[workspace_root] = (now, False)
            return False

        # Walk all .py files — stop early if any is newer than the cache.
        try:
            for py_file in ws_path.rglob("*.py"):
                # Skip hidden dirs and caches.
                parts = py_file.parts
                if any(p.startswith(".") for p in parts):
                    continue
                if "__pycache__" in str(py_file):
                    continue
                try:
                    if py_file.stat().st_mtime > cache_mtime:
                        self._staleness[workspace_root] = (now, True)
                        return True
                except OSError:
                    continue
        except (OSError, PermissionError):
            pass  # Can't walk — assume not stale (don't force a rebuild).

        self._staleness[workspace_root] = (now, False)
        return False

    def get_graph(self, workspace_root: str, *, force_rebuild: bool = False) -> nx.DiGraph:
        """Return the cached graph, auto-rebuilding if workspace files are newer."""
        cache_key = _cache_key(workspace_root)
        cache_path = self._cache_dir / cache_key

        if not force_rebuild and cache_path.exists():
            if self._is_stale(workspace_root, cache_path):
                force_rebuild = True
            else:
                try:
                    with open(cache_path, "rb") as f:
                        return pickle.load(f)
                except (pickle.PickleError, EOFError):
                    pass  # Corrupt cache — rebuild.

        graph = _build_graph(workspace_root)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(graph, f)
        # Invalidate staleness cache after rebuild.
        self._staleness.pop(workspace_root, None)
        return graph

    def clear_cache(self, workspace_root: str) -> None:
        cache_path = self._cache_dir / _cache_key(workspace_root)
        if cache_path.exists():
            cache_path.unlink()


def _cache_key(workspace_root: str) -> str:
    """Stable filename from workspace path."""
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", workspace_root.strip("/"))
    return f"{safe}{GRAPH_EXT}"


def cache_exists(workspace_root: str) -> bool:
    """Check whether a cached graph exists for this workspace."""
    return (CACHE_DIR / _cache_key(workspace_root)).exists()


class GraphQuerier:
    """Queries a pre-built graph for relational code questions."""

    def __init__(self, builder: GraphBuilder | None = None) -> None:
        self._builder = builder or GraphBuilder()
        self._graph: nx.DiGraph | None = None
        self._workspace: str | None = None

    def load(self, workspace_root: str, *, force_rebuild: bool = False) -> None:
        """Load (or build) the graph for a workspace."""
        try:
            self._graph = self._builder.get_graph(workspace_root, force_rebuild=force_rebuild)
            self._workspace = workspace_root
        except Exception as exc:
            raise ReasonerUnavailable(f"Graphify: {exc}") from exc

    def query_calls(self, symbol: str) -> tuple[GrepHit, ...]:
        """What does this symbol call?"""
        return self._query_edges(symbol, "calls", direction="out")

    def query_callers(self, symbol: str) -> tuple[GrepHit, ...]:
        """What calls this symbol?"""
        return self._query_edges(symbol, "calls", direction="in")

    def query_imports(self, symbol: str) -> tuple[GrepHit, ...]:
        """What does this module/function import?"""
        return self._query_edges(symbol, "imports", direction="out")

    def query_subclasses(self, symbol: str) -> tuple[GrepHit, ...]:
        """What inherits from this class?"""
        return self._query_edges(symbol, "inherits", direction="in")

    def query(self, symbol: str, intent: str = "calls") -> tuple[GrepHit, ...]:
        """Generic query — maps intent to the right query method."""
        if self._graph is None:
            return ()
        mapping = {
            "calls": self.query_calls,
            "callers": self.query_callers,
            "imports": self.query_imports,
            "subclasses": self.query_subclasses,
            "blast_radius": self.query_blast_radius,
        }
        method = mapping.get(intent, self.query_calls)
        return method(symbol)

    def query_blast_radius(self, symbol: str) -> tuple[GrepHit, ...]:
        """What depends on this symbol? Returns callers + importers + subclasses."""
        callers = self.query_callers(symbol)
        importers = self._query_edges(symbol, "imports", direction="in")
        subclasses = self.query_subclasses(symbol)
        # Merge and deduplicate by (file, line).
        seen: set[tuple[str, int]] = set()
        merged: list[GrepHit] = []
        for hit in (*callers, *importers, *subclasses):
            key = (hit.file, hit.line)
            if key not in seen:
                seen.add(key)
                merged.append(hit)
        return tuple(merged)

    def query_overview(self) -> tuple[GrepHit, ...]:
        """Structural digest: module map with entry points and key symbols.

        Returns one hit per module (file), with a snippet summarizing its
        exported functions/classes and role in the codebase. Useful for
        catch-up Q&A like "how does X work" or "what are the key modules."
        """
        if self._graph is None:
            return ()
        # Collect modules and their symbols.
        modules: dict[str, dict[str, list[str]]] = {}  # file → {kind: [name, ...]}
        for node_id, data in self._graph.nodes(data=True):
            kind = data.get("kind", "")
            if kind in ("function", "class"):
                file = data.get("file", node_id.split("::")[0])
                name = node_id.rsplit("::", 1)[-1]
                if file not in modules:
                    modules[file] = {}
                if kind not in modules[file]:
                    modules[file][kind] = []
                modules[file][kind].append(name)

        hits: list[GrepHit] = []
        for file, kinds in sorted(modules.items()):
            funcs = kinds.get("function", [])
            classes = kinds.get("class", [])
            parts: list[str] = []
            if funcs:
                # Show at most 5 function names per module.
                shown = funcs[:5]
                suffix = f" +{len(funcs) - 5} more" if len(funcs) > 5 else ""
                parts.append(f"{len(funcs)} function{'s' if len(funcs) != 1 else ''}: {', '.join(shown)}{suffix}")
            if classes:
                shown = classes[:3]
                suffix = f" +{len(classes) - 3} more" if len(classes) > 3 else ""
                parts.append(f"{len(classes)} class{'es' if len(classes) != 1 else ''}: {', '.join(shown)}{suffix}")
            snippet = " · ".join(parts) if parts else f"module {file}"
            hits.append(GrepHit(file=file, line=1, column=0, snippet=snippet[:300]))

        return tuple(hits)

    def query_dead_code(self) -> tuple[GrepHit, ...]:
        """Find functions/classes with zero callers (likely dead code)."""
        if self._graph is None:
            return ()
        hits: list[GrepHit] = []
        for node_id in self._graph.nodes:
            node_data = self._graph.nodes[node_id]
            if node_data.get("kind") not in ("function", "class"):
                continue
            # Skip dunder methods — they're called implicitly by Python.
            short_name = node_id.rsplit("::", 1)[-1]
            if short_name.startswith("__") and short_name.endswith("__"):
                continue
            # A symbol is dead if it has no incoming call/inherit edges.
            in_edges = self._graph.in_edges(node_id, data=True)
            has_callers = any(d.get("kind") in ("calls", "inherits") for _, _, d in in_edges)
            if not has_callers:
                file = node_data.get("file", node_id.split("::")[0])
                line = node_data.get("line", 0)
                snippet = node_data.get("snippet", node_id)[:300]
                hits.append(GrepHit(file=file, line=line, column=0, snippet=snippet))
        return tuple(hits)

    def _query_edges(self, symbol: str, edge_kind: str, *,
                     direction: str = "out") -> tuple[GrepHit, ...]:
        if self._graph is None or self._workspace is None:
            return ()
        hits: list[GrepHit] = []
        # Resolve the symbol to a node.
        node_id = _resolve_node(symbol, self._graph)
        if node_id is None:
            return ()

        if direction == "out":
            edges = self._graph.out_edges(node_id, data=True)
            neighbors = [(v, d) for u, v, d in edges if d.get("kind") == edge_kind]
        else:
            edges = self._graph.in_edges(node_id, data=True)
            neighbors = [(u, d) for u, v, d in edges if d.get("kind") == edge_kind]

        for target, data in neighbors:
            target_data = self._graph.nodes.get(target, {})
            target_file = target_data.get("file", target.split("::")[0])
            target_line = data.get("line", target_data.get("line", 0))
            target_snippet = target_data.get("snippet", f"{target} ({data.get('kind', '?')})")
            label = data.get("label", "")
            snippet = f"{target_snippet}"
            if label:
                snippet = f"imports {label}" if edge_kind == "imports" else f"{label} → {snippet}"
            hits.append(GrepHit(
                file=target_file,
                line=target_line,
                column=0,
                snippet=snippet[:300],
            ))
        return tuple(hits)


def _resolve_node(symbol: str, graph: nx.DiGraph) -> str | None:
    """Find a node in the graph matching the symbol name or file."""
    # Exact match: ::symbol
    for node_id in graph.nodes:
        if node_id.endswith(f"::{symbol}"):
            return node_id
    # Case-insensitive.
    lower = symbol.lower()
    for node_id in graph.nodes:
        if node_id.lower().endswith(f"::{lower}"):
            return node_id
    # Partial file match: filename contains the symbol.
    # E.g. symbol="imports_test" → matches "imports_test.py::<module>"
    for node_id in graph.nodes:
        file_part = node_id.split("::")[0] if "::" in node_id else ""
        if file_part and symbol.lower() in file_part.lower():
            return node_id
    return None
