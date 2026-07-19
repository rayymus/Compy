"""Graphify — persistent code-knowledge graph for relational queries.

Uses tree-sitter with multi-language support (Python, JavaScript, TypeScript,
Rust, Go) to parse source files and build a NetworkX DiGraph
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

# Lazy imports — tree-sitter grammars are only imported when _build_graph()
# is actually called. Each language grammar is loaded on demand via _ensure_language().
# This prevents import-time crashes from mismatched versions and missing optional grammars.
_lazy_imports_done = False
_Language = None
_Parser = None
_Query = None
_QueryCursor = None
_lang_modules: dict[str, object] = {}  # language name → grammar module


def _ensure_imports() -> None:
    """Load the core tree-sitter types. Called once."""
    global _lazy_imports_done, _Language, _Parser, _Query, _QueryCursor
    if _lazy_imports_done:
        return
    try:
        from tree_sitter import Language as _Language
        from tree_sitter import Parser as _Parser
        from tree_sitter import Query as _Query
        from tree_sitter import QueryCursor as _QueryCursor
    except ImportError as exc:
        raise ReasonerUnavailable(
            f"Graphify requires tree-sitter: {exc}. "
            f"Install with: pip install tree-sitter"
        ) from exc
    _lazy_imports_done = True


def _load_lang_module(lang_name: str) -> object | None:
    """Lazy-load a tree-sitter language grammar module. Returns None if unavailable."""
    _ensure_imports()
    if lang_name in _lang_modules:
        return _lang_modules[lang_name]
    mapping = {
        "python": "tree_sitter_python",
        "javascript": "tree_sitter_javascript",
        "typescript": "tree_sitter_typescript",
        "rust": "tree_sitter_rust",
        "go": "tree_sitter_go",
    }
    pkg = mapping.get(lang_name)
    if pkg is None:
        return None
    try:
        mod = __import__(pkg)
        # TypeScript has multiple language getters
        if lang_name == "typescript":
            getter = getattr(mod, "language_typescript", None)
        else:
            getter = getattr(mod, "language", None)
        if getter is None:
            return None
        _lang_modules[lang_name] = mod
        return mod
    except ImportError:
        return None

# Cache path — one graph per workspace root.
CACHE_DIR = Path.home() / ".compy"
GRAPH_EXT = ".graph.pkl"

# ── Multi-language tree-sitter queries (§1, claude-response5) ──
# Each language has its own AST node types and queries.  Nodes and edges are
# normalized to a common schema (kind=function/class/module/unknown) so the
# GraphQuerier works identically across languages.

# Per-language definition node types (for _enclosing_symbol).
_DEF_TYPES: dict[str, tuple[str, ...]] = {
    "python": ("function_definition", "class_definition"),
    "javascript": ("function_declaration", "class_declaration", "method_definition"),
    "typescript": ("function_declaration", "class_declaration", "method_definition"),
    "rust": ("function_item", "struct_item", "enum_item", "trait_item", "impl_item"),
    "go": ("function_declaration", "type_declaration", "method_declaration"),
}

_DEF_QUERIES: dict[str, str] = {
    "python": """
(function_definition
  name: (identifier) @func.name) @func.def
(class_definition
  name: (identifier) @class.name) @class.def
""",
    "javascript": """
(function_declaration
  name: (identifier) @func.name) @func.def
(class_declaration
  name: (identifier) @class.name) @class.def
(method_definition
  name: (property_identifier) @func.name) @func.def
""",
    "typescript": """
(function_declaration
  name: (identifier) @func.name) @func.def
(class_declaration
  name: (type_identifier) @class.name) @class.def
(method_definition
  name: (property_identifier) @func.name) @func.def
""",
    "rust": """
(function_item
  name: (identifier) @func.name) @func.def
(struct_item
  name: (type_identifier) @class.name) @class.def
(enum_item
  name: (type_identifier) @class.name) @class.def
(trait_item
  name: (type_identifier) @class.name) @class.def
(impl_item
  type: (type_identifier) @class.name) @class.def
""",
    "go": """
(function_declaration
  name: (identifier) @func.name) @func.def
(method_declaration
  name: (field_identifier) @func.name) @func.def
(type_declaration
  name: (type_identifier) @class.name) @class.def
""",
}

_CALL_QUERIES: dict[str, str] = {
    "python": """
(call
  function: (identifier) @call.name) @call.expr
(call
  function: (attribute
    object: (identifier) @call.obj
    attribute: (identifier) @call.attr)) @call.expr
""",
    "javascript": """
(call_expression
  function: (identifier) @call.name) @call.expr
(call_expression
  function: (member_expression
    property: (property_identifier) @call.prop)) @call.expr
""",
    "typescript": """
(call_expression
  function: (identifier) @call.name) @call.expr
(call_expression
  function: (member_expression
    property: (property_identifier) @call.prop)) @call.expr
""",
    "rust": """
(call_expression
  function: (identifier) @call.name) @call.expr
(call_expression
  function: (field_expression
    field: (field_identifier) @call.prop)) @call.expr
""",
    "go": """
(call_expression
  function: (identifier) @call.name) @call.expr
(call_expression
  function: (selector_expression
    field: (field_identifier) @call.prop)) @call.expr
""",
}

_IMPORT_QUERIES: dict[str, str] = {
    "python": """
(import_statement
  name: (dotted_name) @import.name) @import.stmt
(import_from_statement
  module_name: (dotted_name) @import.from
  name: (dotted_name) @import.name) @import.stmt
""",
    "javascript": """
(import_statement) @import.stmt
(lexical_declaration
  (variable_declarator
    name: (identifier) @import.name
    value: (call_expression
      function: (identifier) @_req
      arguments: (arguments (string) @import.from))) @import.stmt
  (#eq? @_req "require"))
""",
    "typescript": """
(import_statement) @import.stmt
""",
}

_INHERIT_QUERIES: dict[str, str] = {
    "python": """
(class_definition
  name: (identifier) @class.name
  superclasses: (argument_list
    (identifier) @parent.name)) @class.def
""",
    "javascript": """
(class_declaration
  name: (identifier) @class.name) @class.def
""",
    "typescript": """
(class_declaration
  name: (type_identifier) @class.name) @class.def
""",
}

# File extension → language name mapping.
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
}

def _build_graph(workspace_root: str) -> nx.DiGraph:
    """Build a NetworkX DiGraph from all supported source files in workspace_root.

    Supports Python, JavaScript, TypeScript, Rust, and Go via tree-sitter.
    Each file is parsed with its language-specific grammar; nodes and edges
    are normalized to a common schema so the GraphQuerier works identically
    across all languages.
    """
    _ensure_imports()
    graph = nx.DiGraph()
    ws_path = Path(workspace_root)

    # Group files by language for one-parser-per-language efficiency.
    lang_files: dict[str, list[Path]] = {}
    for ext, lang in _EXT_TO_LANG.items():
        pattern = f"*{ext}"
        for fp in ws_path.rglob(pattern):
            # Skip hidden dirs, node_modules, and caches.
            parts = fp.parts
            if any(p.startswith(".") for p in parts):
                continue
            if any(skip in parts for skip in ("node_modules", "__pycache__", "target", "vendor")):
                continue
            lang_files.setdefault(lang, []).append(fp)

    # Process each language separately.
    for lang_name, files in sorted(lang_files.items()):
        mod = _load_lang_module(lang_name)
        if mod is None:
            continue  # Grammar not installed — silently skip this language.

        # Get the language object.
        if lang_name == "typescript":
            getter = getattr(mod, "language_typescript", None)
        else:
            getter = getattr(mod, "language", None)
        if getter is None:
            continue
        lang = _Language(getter())
        parser = _Parser(lang)

        # Compile language-specific queries (or skip ones not defined).
        def_q = _Query(lang, _DEF_QUERIES[lang_name]) if lang_name in _DEF_QUERIES else None
        call_q = _Query(lang, _CALL_QUERIES[lang_name]) if lang_name in _CALL_QUERIES else None
        import_q = _Query(lang, _IMPORT_QUERIES[lang_name]) if lang_name in _IMPORT_QUERIES else None
        inherit_q = _Query(lang, _INHERIT_QUERIES[lang_name]) if lang_name in _INHERIT_QUERIES else None

        def_types = _DEF_TYPES.get(lang_name, ("function_definition", "class_definition"))

        for file_path in files:
            try:
                source = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            tree = parser.parse(source.encode("utf-8"))
            rel_path = str(file_path.relative_to(workspace_root))

            # --- definitions (functions + classes) ---
            if def_q:
                def_cur = _QueryCursor(def_q)
                for cap_name, cap_nodes in def_cur.captures(tree.root_node).items():
                    for cap_node in cap_nodes:
                        if cap_name in ("func.name",):
                            node_text = cap_node.text.decode("utf-8")
                            node_id = f"{rel_path}::{node_text}"
                            graph.add_node(node_id, kind="function", file=rel_path,
                                           line=cap_node.start_point[0] + 1,
                                           snippet=_node_snippet(cap_node, source),
                                           language=lang_name)
                        elif cap_name in ("class.name",):
                            node_text = cap_node.text.decode("utf-8")
                            node_id = f"{rel_path}::{node_text}"
                            graph.add_node(node_id, kind="class", file=rel_path,
                                           line=cap_node.start_point[0] + 1,
                                           snippet=_node_snippet(cap_node, source),
                                           language=lang_name)

            # --- calls ---
            if call_q:
                call_cur = _QueryCursor(call_q)
                for cap_name, cap_nodes in call_cur.captures(tree.root_node).items():
                    for cap_node in cap_nodes:
                        if cap_name == "call.expr":
                            caller = _enclosing_symbol(cap_node, tree.root_node, source, rel_path, graph, def_types)
                            if caller is None:
                                continue
                            callee_name = _extract_call_name(cap_node, source, lang_name)
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
                                                   snippet=f"unresolved: {callee_name}",
                                                   language=lang_name)
                                graph.add_edge(caller, unresolved, kind="calls",
                                               line=cap_node.start_point[0] + 1)

            # --- imports ---
            if import_q:
                import_cur = _QueryCursor(import_q)
                for cap_name, cap_nodes in import_cur.captures(tree.root_node).items():
                    for cap_node in cap_nodes:
                        if cap_name == "import.stmt":
                            enclosing = _enclosing_module(tree.root_node, source, rel_path, graph, lang_name)
                            if enclosing is None:
                                continue
                            imported = cap_node.text.decode("utf-8")
                            graph.add_edge(enclosing, f"<import>::{imported}", kind="imports",
                                           line=cap_node.start_point[0] + 1, label=imported)

            # --- inheritance ---
            if inherit_q:
                inherit_cur = _QueryCursor(inherit_q)
                for cap_name, cap_nodes in inherit_cur.captures(tree.root_node).items():
                    for cap_node in cap_nodes:
                        if cap_name == "parent.name":
                            parent_name = cap_node.text.decode("utf-8")
                            enclosing = _enclosing_symbol(cap_node, tree.root_node, source, rel_path, graph, def_types)
                            if enclosing:
                                parent_id = _resolve_callee(parent_name, rel_path, graph)
                                if parent_id:
                                    graph.add_edge(enclosing, parent_id, kind="inherits",
                                                   line=cap_node.start_point[0] + 1)
                        elif cap_name == "class.def" and lang_name in ("javascript", "typescript"):
                            # JS/TS: manually walk class_declaration children for class_heritage → identifier
                            class_name = _extract_class_name(cap_node)
                            parent_name = _extract_js_parent(cap_node)
                            if class_name and parent_name:
                                class_id = f"{rel_path}::{class_name}"
                                parent_id = _resolve_callee(parent_name, rel_path, graph)
                                if parent_id and graph.has_node(class_id):
                                    graph.add_edge(class_id, parent_id, kind="inherits",
                                                   line=cap_node.start_point[0] + 1)

    return graph




def _extract_class_name(node: Any) -> str | None:
    """Extract the class name from a class_declaration node."""
    for child in node.children:
        if child.type in ("identifier", "type_identifier"):
            return child.text.decode("utf-8")
    return None


def _extract_js_parent(node: Any) -> str | None:
    """Walk a class_declaration node to find the parent class in extends clause.

    JS/TS AST: class_declaration → class_heritage → identifier (the parent name).
    """
    for child in node.children:
        if child.type == "class_heritage":
            for gc in child.children:
                if gc.type == "identifier":
                    return gc.text.decode("utf-8")
    return None

def _node_snippet(node: Any, source: str) -> str:
    """Extract the first line of a node as a snippet."""
    start = node.start_point[0]
    lines = source.splitlines()
    if start < len(lines):
        return lines[start][:200]
    return node.text.decode("utf-8")[:200]


def _enclosing_symbol(node: Any, root: Any, source: str, rel_path: str,
                      graph: nx.DiGraph,
                      def_types: tuple[str, ...] = ("function_definition", "class_definition")) -> str | None:
    """Walk up the tree to find the enclosing function or class.

    def_types is language-specific — JS uses function_declaration, Rust uses
    function_item, etc. Defaults to Python's types for backward compatibility.
    """
    current: Any | None = node
    while current is not None:
        if current.type in def_types:
            # Find the name child (varies by language: identifier, property_identifier, type_identifier).
            for child in current.children:
                if child.type in ("identifier", "property_identifier", "type_identifier", "field_identifier"):
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
                      graph: nx.DiGraph, lang_name: str = "") -> str | None:
    """Get the module-level node for a file."""
    module_id = f"{rel_path}::<module>"
    if not graph.has_node(module_id):
        graph.add_node(module_id, kind="module", file=rel_path, line=1,
                       snippet=f"module {rel_path}", language=lang_name)
    return module_id


def _extract_call_name(node: Any, source: str, lang_name: str = "python") -> str | None:
    """Extract the callee name from a call expression.

    Handles Python (call→identifier/attribute), JS/TS (call_expression→identifier/member_expression),
    Rust (call_expression→identifier/field_expression), and Go (call_expression→identifier/selector_expression).
    """
    for child in node.children:
        if child.type in ("identifier",):
            return child.text.decode("utf-8")
        elif child.type in ("attribute",):
            # Python: obj.method() — return method name
            ids = [c.text.decode("utf-8") for c in child.children if c.type == "identifier"]
            if ids:
                return ids[-1]
        elif child.type in ("member_expression",):
            # JS/TS: obj.method() — return property name
            for c in child.children:
                if c.type in ("property_identifier",):
                    return c.text.decode("utf-8")
        elif child.type in ("field_expression",):
            # Rust: obj.method()
            for c in child.children:
                if c.type in ("field_identifier",):
                    return c.text.decode("utf-8")
        elif child.type in ("selector_expression",):
            # Go: obj.method()
            for c in child.children:
                if c.type in ("field_identifier",):
                    return c.text.decode("utf-8")
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

        # Walk all supported source files — stop early if any is newer than the cache.
        try:
            for ext, lang in _EXT_TO_LANG.items():
                if _load_lang_module(lang) is None:
                    continue  # Grammar not installed — skip this language.
                for sf in ws_path.rglob(f"*{ext}"):
                    parts = sf.parts
                    if any(p.startswith(".") for p in parts):
                        continue
                    if any(skip in parts for skip in ("node_modules", "__pycache__", "target", "vendor")):
                        continue
                    try:
                        if sf.stat().st_mtime > cache_mtime:
                            self._staleness[workspace_root] = (now, True)
                            return True
                    except OSError:
                        continue
        except (OSError, PermissionError):
            pass  # Can't walk — assume not stale (don't force a rebuild).

        self._staleness[workspace_root] = (now, False)
        return False

    def get_graph(self, workspace_root: str, *, force_rebuild: bool = False, fast_only: bool = False) -> nx.DiGraph:
        """Return the cached graph, auto-rebuilding if workspace files are newer.

        When `fast_only=True`, skips staleness checks and rebuilds entirely —
        returns the cached pickle if it exists, raises otherwise.  Used for
        reasoner enrichment where graph building would block the query pipeline.
        """
        cache_key = _cache_key(workspace_root)
        cache_path = self._cache_dir / cache_key

        if fast_only:
            if not cache_path.exists():
                raise ReasonerUnavailable(f"Graphify: no cached graph for {workspace_root}")
            try:
                with open(cache_path, "rb") as f:
                    return pickle.load(f)
            except (pickle.PickleError, EOFError) as exc:
                raise ReasonerUnavailable(f"Graphify: corrupt cache: {exc}") from exc

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

    def load(self, workspace_root: str, *, force_rebuild: bool = False, fast_only: bool = False) -> None:
        """Load (or build) the graph for a workspace."""
        try:
            self._graph = self._builder.get_graph(workspace_root, force_rebuild=force_rebuild, fast_only=fast_only)
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
            "path": self.query_path,
        }
        method = mapping.get(intent, self.query_calls)
        return method(symbol)

    def query_path(self, source_symbol: str, target_symbol: str = "") -> tuple[GrepHit, ...]:
        """Shortest path between two symbols in the call graph.

        When called via the generic `query(symbol, intent="path")`, the symbol
        string encodes both endpoints as "source::target".  Direct callers should
        pass source_symbol and target_symbol explicitly.
        """
        if self._graph is None:
            return ()
        # Support both encoding styles: "src::tgt" from orchestrator, or explicit args.
        if "::" in source_symbol and not target_symbol:
            parts = source_symbol.split("::", 1)
            source_symbol, target_symbol = parts[0], parts[1]
        src = _resolve_node(source_symbol, self._graph)
        tgt = _resolve_node(target_symbol, self._graph)
        if src is None or tgt is None:
            return ()
        if src == tgt:
            return ()  # Same symbol — no path to show.
        try:
            path = nx.shortest_path(self._graph, source=src, target=tgt)
            # Build a compact chain: "func1 → func2 → func3"
            names: list[str] = []
            for n in path:
                data = self._graph.nodes.get(n, {})
                short = n.rsplit("::", 1)[-1]
                names.append(short)
            path_str = " → ".join(names)
            first_node = self._graph.nodes[path[0]]
            file = first_node.get("file", "")
            line = first_node.get("line", 0)
            snippet = first_node.get("snippet", path_str)[:300]
            return (GrepHit(
                file=file, line=line, column=0,
                snippet=f"Path ({len(path) - 1} hops): {path_str}\
{snippet}",
            ),)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return ()

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
