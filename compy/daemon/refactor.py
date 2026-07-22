"""Tier 1 refactoring: external formatters through a shared apply pipeline.

Architecture boundary per claude-response3.md §5: the Reasoner Protocol stays
read-only/scoring-only — edit generation and file writes live here, entirely
separate from the search pipeline.

Shared pipeline: generate → pre-write verify → stage → confirm → backup →
atomic write → post-write verify → register for undo.

Stateless design: daemon stages proposed edits to /tmp/compy-staged-<token>.json,
returns the token in QueryResult.  The overlay spawns a second daemon invocation
with "/confirm <token>" to apply, or discards the staged file on reject.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import FileProposal, QueryResult, Selection

# ── Formatter detection ────────────────────────────────────────────────

# Map file extensions to formatter commands.  Each entry is
# (executable, *args) where the last arg is a placeholder for stdin.
_FORMATTERS: dict[str, list[str]] = {
    ".py": ["black", "--quiet", "-"],
    ".js": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".ts": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".jsx": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".tsx": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".json": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
    ".md": ["npx", "--quiet", "prettier", "--stdin-filepath", "{file}"],
}


def _detect_formatter(file_path: str) -> list[str] | None:
    """Return the formatter command for a file, or None if unsupported."""
    ext = Path(file_path).suffix.lower()
    return _FORMATTERS.get(ext)


def _is_formatter_available(cmd: list[str]) -> bool:
    """Check whether the formatter executable is on PATH."""
    exe = cmd[0]
    return shutil.which(exe) is not None


# ── Pre-write verification ──────────────────────────────────────────────

def _verify_syntax(file_path: str, content: str) -> str | None:
    """Tree-sitter parse-check proposed content.  Returns None on success,
    or an error message string on failure."""
    ext = Path(file_path).suffix.lower()
    if ext == ".py":
        return _verify_python(content)
    # For JS/TS/JSON/MD: skip tree-sitter parse — Prettier output is
    # syntactically valid by construction.  A failed parse would mean
    # Prettier itself is broken, which is outside our scope.
    return None


def _verify_python(source: str) -> str | None:
    """Parse-check Python source via tree-sitter.  Returns None on success."""
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except Exception:
        return None  # tree-sitter not available — skip verify
    try:
        lang = Language(tspython.language())
        parser = Parser(lang)
        tree = parser.parse(source.encode("utf-8"))
        if tree.root_node.has_error:
            return "Python syntax error in proposed content"
    except Exception as exc:
        return f"Syntax check failed: {exc}"
    return None


# ── Tier 2: AST-scoped rename ───────────────────────────────────────────

def stage_rename(
    old_name: str, new_name: str, workspace: str, grapher,
) -> QueryResult | None:
    """Generate rename proposals: find every identifier AST node matching
    old_name via Graphify callers + definition, tree-sitter-scoped.

    Returns QueryResult with multiple FileProposals + one refactor_token,
    or None if no references found / tree-sitter unavailable.
    """
    from .graphify import _resolve_node

    _cleanup_stale_staged()

    # 1. Collect affected files: definition site + all callers.
    affected: set[str] = set()
    try:
        grapher.load(workspace)
    except Exception:
        return None  # Graphify unavailable.

    # Find the definition node's file via the graph.
    graph = grapher._graph  # type: ignore[attr-defined]
    if graph is not None:
        def_node = _resolve_node(old_name, graph)
        if def_node is not None:
            def_file = graph.nodes[def_node].get("file", "")
            if def_file:
                affected.add(def_file)

    # Add all caller files.
    callers = grapher.query(old_name, intent="callers")  # type: ignore[union-attr]
    for h in callers:
        affected.add(h.file)

    if not affected:
        return None

    # 2. For each affected file, find identifier AST nodes matching old_name.
    edits: list[_StagedEdit] = []
    for rel_path in affected:
        file_path = (Path(workspace).resolve() / rel_path)
        if not file_path.exists() or file_path.suffix != ".py":
            continue
        try:
            result = _rename_in_file(str(file_path), old_name, new_name)
        except Exception:
            continue
        if result is not None:
            edits.append(result)

    if not edits:
        return None

    # 3. Stage all edits under one token.
    token = _make_token()
    stage_path = STAGE_DIR / f"compy-staged-{token}.json"
    stage_path.write_text(
        json.dumps([asdict(e) for e in edits]), encoding="utf-8",
    )

    proposals = tuple(
        FileProposal(
            file=str(Path(e.file).resolve().relative_to(Path(workspace).resolve())),
            changed_lines=abs(e.original.count("\n") - e.formatted.count("\n")) + 1,
        )
        for e in edits
    )

    return QueryResult(
        intent="rename",
        refactor_proposals=proposals,
        refactor_token=token,
        reason=f"Staged rename of '{old_name}' → '{new_name}'. Note: string, comment, and config references skipped for safety.",
    )


def _rename_in_file(file_path: str, old_name: str, new_name: str) -> _StagedEdit | None:
    """Parse a file with tree-sitter, find all identifier nodes matching
    old_name, and produce the renamed content via bottom-up replacement.

    Returns a _StagedEdit with original + formatted, or None if no matches.
    """
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser, Query
    except Exception:
        return None  # tree-sitter unavailable.

    try:
        original_bytes = Path(file_path).read_bytes()
    except OSError:
        return None

    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(original_bytes)

    # Query for all identifier nodes, filter by text match.
    id_query = Query(lang, "(identifier) @id")
    matches: list[tuple[int, int]] = []  # (start_byte, end_byte)

    for cap_name, cap_nodes in id_query.captures(tree.root_node).items():
        for node in cap_nodes:
            if node.text is not None and node.text.decode("utf-8") == old_name:
                matches.append((node.start_byte, node.end_byte))

    if not matches:
        return None

    # Sort bottom-up (reverse byte order) for safe replacement.
    matches.sort(key=lambda m: m[0], reverse=True)

    # Apply replacements.
    result = bytearray(original_bytes)
    new_bytes = new_name.encode("utf-8")
    for start, end in matches:
        result[start:end] = new_bytes

    formatted = result.decode("utf-8")
    return _StagedEdit(
        file=file_path,
        original=original_bytes.decode("utf-8"),
        formatted=formatted,
    )


def _cleanup_stale_staged() -> None:
    """Remove staged edit files older than 5 minutes — orphans from rejected proposals."""
    cutoff = time.time() - 300  # 5 minutes
    try:
        for p in STAGE_DIR.glob("compy-staged-*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


# ── Staging / applying ──────────────────────────────────────────────────

STAGE_DIR = Path("/tmp")
UNDO_PATH = STAGE_DIR / "compy-undo.json"


def _make_token() -> str:
    """Short unique token for staged-edit filenames."""
    raw = f"{time.time():.6f}-{os.getpid()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


@dataclass
class _StagedEdit:
    file: str
    original: str  # original file content (utf-8 text)
    formatted: str  # proposed new content


def stage_format(selection: Selection, workspace: str) -> QueryResult | None:
    """Generate format proposals for the selected file.

    Returns a QueryResult with refactor_proposals + refactor_token on success,
    or None if no formatter is available / nothing changed.

    Cleans up orphaned staged files older than 5 minutes before creating new ones.
    """
    # Clean up stale staged files from rejected proposals.
    _cleanup_stale_staged()
    sel_file = selection.file
    if not sel_file:
        return None

    # Resolve against workspace.
    file_path = (Path(workspace).resolve() / sel_file)
    if not file_path.exists():
        return None

    cmd_template = _detect_formatter(str(file_path))
    if cmd_template is None:
        return None

    # Resolve {file} placeholder.
    cmd = [str(file_path) if a == "{file}" else a for a in cmd_template]
    if not _is_formatter_available(cmd):
        return None

    # Read original content.
    try:
        original = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    # Run formatter: pipe original to stdin, capture stdout.
    try:
        result = subprocess.run(
            cmd, input=original, capture_output=True, text=True, timeout=15,
        )
        formatted = result.stdout
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return None

    if formatted == original or not formatted.strip():
        return None  # Nothing changed or empty output.

    # Pre-write verify.
    err = _verify_syntax(str(file_path), formatted)
    if err:
        return QueryResult(
            intent="format",
            degraded=True,
            reason=f"Pre-write verify failed: {err}",
        )

    # Stage the edit.
    token = _make_token()
    staged = _StagedEdit(file=str(file_path), original=original, formatted=formatted)
    stage_path = STAGE_DIR / f"compy-staged-{token}.json"
    stage_path.write_text(json.dumps(asdict(staged)), encoding="utf-8")

    # Count approximate changed lines.
    orig_lines = original.count("\n")
    new_lines = formatted.count("\n")
    changed = abs(new_lines - orig_lines)

    # Compute unified diff for in-overlay preview.
    diff = "\n".join(difflib.unified_diff(
        original.splitlines(), formatted.splitlines(),
        fromfile=f"a/{sel_file}", tofile=f"b/{sel_file}",
        lineterm="",
    ))
    # Cap diff at ~100 lines to bound JSON size.
    diff_lines = diff.split("\n")
    if len(diff_lines) > 100:
        diff = "\n".join(diff_lines[:100]) + "\n... (truncated)"
    diff_preview = diff if diff else None

    return QueryResult(
        intent="format",
        refactor_proposals=(FileProposal(file=sel_file, changed_lines=changed, diff_preview=diff_preview),),
        refactor_token=token,
    )


def apply_staged(token: str, workspace: str) -> QueryResult:
    """Apply a previously staged edit: atomic write → backup → post-verify.

    Called by the orchestrator when it receives "/confirm <token>".
    Backup (undo registration) happens AFTER successful write, so a failed
    write never leaves a dangling undo entry pointing to an unchanged file.
    """
    stage_path = STAGE_DIR / f"compy-staged-{token}.json"
    if not stage_path.exists():
        return QueryResult(
            intent="format", degraded=True,
            reason=f"Staged edit expired or not found: {token}",
        )

    try:
        data = json.loads(stage_path.read_text(encoding="utf-8"))
        # Support both single-edit (Tier 1 format) and multi-edit (Tier 2 rename).
        edits: list[_StagedEdit] = (
            [_StagedEdit(**data)] if isinstance(data, dict)
            else [_StagedEdit(**e) for e in data]
        )
    except (json.JSONDecodeError, TypeError):
        return QueryResult(
            intent="format", degraded=True,
            reason="Corrupt staged edit file.",
        )

    applied: list[str] = []
    errors: list[str] = []
    for edit in edits:
        file_path = Path(edit.file)
        if not file_path.exists():
            errors.append(f"{edit.file}: no longer exists")
            continue

        # Atomic write: write to temp, rename over original.
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(file_path.parent), prefix=".compy-", suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(edit.formatted)
            os.replace(tmp_name, str(file_path))  # atomic on same filesystem
        except OSError as exc:
            errors.append(f"{edit.file}: write failed: {exc}")
            continue

        # Backup: snapshot pre-edit bytes for undo (AFTER successful write).
        _register_undo(edit)

        # Post-write verify: re-read from disk and parse-check.
        try:
            written = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{edit.file}: post-write read failed: {exc}")
            continue
        err = _verify_syntax(str(file_path), written)
        if err:
            errors.append(f"{edit.file}: post-write verify failed: {err}")
            continue
        applied.append(edit.file)

    # Clean up staged file.
    stage_path.unlink(missing_ok=True)

    if errors:
        return QueryResult(
            intent="format", degraded=True,
            reason=f"Applied {len(applied)} files, {len(errors)} failed: {'; '.join(errors[:3])}",
        )
    return QueryResult(
        intent="format",
        hits=(),
        reason=f"Applied {len(applied)} file{'s' if len(applied) != 1 else ''}",
    )


# ── Undo ─────────────────────────────────────────────────────────────────

def _register_undo(edit: _StagedEdit) -> None:
    """Record the original content so /undo can restore it."""
    entries: list[dict] = []
    if UNDO_PATH.exists():
        try:
            entries = json.loads(UNDO_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            entries = []
    entries.append({
        "ts": time.time(),
        "file": edit.file,
        "original": edit.original,
    })
    UNDO_PATH.write_text(json.dumps(entries), encoding="utf-8")


def undo_last() -> QueryResult:
    """Restore every file from the most recent undo snapshot.

    One undo restores ALL files from that operation — not per-file.
    """
    if not UNDO_PATH.exists():
        return QueryResult(
            intent="format", degraded=True,
            reason="Nothing to undo.",
        )

    try:
        entries = json.loads(UNDO_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return QueryResult(
            intent="format", degraded=True,
            reason="Corrupt undo file.",
        )

    if not entries:
        return QueryResult(
            intent="format", degraded=True,
            reason="Nothing to undo.",
        )

    # Group by timestamp — undo the most recent batch (same second).
    groups: dict[int, list[dict]] = {}
    for e in entries:
        ts = int(e["ts"])
        groups.setdefault(ts, []).append(e)
    latest_ts = max(groups)
    batch = groups[latest_ts]

    restored: list[str] = []
    errors: list[str] = []
    for e in batch:
        file_path = Path(e["file"])
        try:
            file_path.write_text(e["original"], encoding="utf-8")
            restored.append(e["file"])
        except OSError as exc:
            errors.append(f"{e['file']}: {exc}")

    # Remove the undone entries from the undo file.
    remaining = [e for e in entries if int(e["ts"]) != latest_ts]
    if remaining:
        UNDO_PATH.write_text(json.dumps(remaining), encoding="utf-8")
    else:
        UNDO_PATH.unlink(missing_ok=True)

    if errors:
        return QueryResult(
            intent="format", degraded=True,
            reason=f"Undo restored {len(restored)} files, {len(errors)} failed: {'; '.join(errors[:3])}",
        )
    return QueryResult(
        intent="format",
        hits=(),
        reason=f"Undid {len(restored)} file{'s' if len(restored) != 1 else ''}: {', '.join(restored)}",
    )


# ── Tier 3: Inline Suggestions (deterministic, no LLM) ───────────────────

def _stage_single_file_edit(
    file_path: str, original: str, formatted: str, intent: str,
) -> QueryResult:
    """Stage a single-file edit through the existing confirm/apply pipeline.

    Reuses _make_token, _StagedEdit, and STAGE_DIR from the Tier 1-2 pipeline.
    """
    _cleanup_stale_staged()
    if formatted == original:
        return QueryResult(intent=intent, degraded=True, reason="No changes needed.")

    token = _make_token()
    staged = _StagedEdit(file=file_path, original=original, formatted=formatted)
    stage_path = STAGE_DIR / f"compy-staged-{token}.json"
    stage_path.write_text(json.dumps(asdict(staged)), encoding="utf-8")

    changed = abs(original.count("\n") - formatted.count("\n")) + 1

    # Compute unified diff for in-overlay preview.
    file_display = file_path.rsplit("/", 1)[-1]
    diff = "\n".join(difflib.unified_diff(
        original.splitlines(), formatted.splitlines(),
        fromfile=f"a/{file_display}", tofile=f"b/{file_display}",
        lineterm="",
    ))
    # Cap diff at ~100 lines to bound JSON size.
    diff_lines = diff.split("\n")
    if len(diff_lines) > 100:
        diff = "\n".join(diff_lines[:100]) + "\n... (truncated)"
    diff_preview = diff if diff else None

    return QueryResult(
        intent=intent,
        refactor_proposals=(FileProposal(file=file_path, changed_lines=changed, diff_preview=diff_preview),),
        refactor_token=token,
        reason=f"Staged {intent}. Enter to confirm, Esc to reject.",
    )


def stage_extract_variable(
    selection: Selection, workspace: str
) -> QueryResult | None:
    """Extract selected expression to 'extracted_var' on the preceding line.

    Deterministic, no LLM. Simple string manipulation: find the line containing
    the selection, insert variable assignment above, replace expression.
    """
    sel_file = selection.file
    sel_line = selection.line
    sel_text = selection.text
    if not sel_file or not sel_line or not sel_text:
        return None

    expr = sel_text.strip()
    if not expr or "\n" in expr:
        return None  # multi-line selection not supported in v1

    file_path = Path(workspace).resolve() / sel_file
    try:
        original = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    lines = original.splitlines()
    target_idx = sel_line - 1
    if target_idx < 0 or target_idx >= len(lines):
        return None

    line_text = lines[target_idx]
    if expr not in line_text:
        return None  # expression not found on target line

    indent = line_text[:len(line_text) - len(line_text.lstrip())]
    var_name = _suggest_variable_name(expr)

    # Replace ONLY the first occurrence — avoids corrupting the line
    # if the expression text appears multiple times.
    new_line = line_text.replace(expr, var_name, 1)
    new_assignment = f"{indent}{var_name} = {expr}"

    lines[target_idx] = new_line
    lines.insert(target_idx, new_assignment)

    trailing = "\n" if original.endswith("\n") else ""
    formatted = "\n".join(lines) + trailing

    return _stage_single_file_edit(
        str(file_path), original, formatted, intent="extract_variable",
    )


def _suggest_variable_name(expr: str) -> str:
    """Heuristic variable name from expression. Returns a readable name."""
    # Strip parens, brackets, and common wrappers.
    clean = expr.strip().lstrip("({").rstrip(")}")
    # If it's a function call, use the function name.
    if "(" in clean:
        return clean.split("(")[0].strip().split(".")[-1] + "_result"
    # If it looks like a computation, use 'result'.
    if any(op in clean for op in ("+", "-", "*", "/", "%", "&", "|")):
        return "result"
    # Fallback: snake_case, truncated. Ensure result is a valid Python identifier.
    name = clean.replace(".", "_").replace("[", "_").replace("]", "")
    name = name[:20].strip("_") or "extracted"
    # Guard: bare numbers and other non-identifiers.
    if not name.isidentifier():
        return "extracted"
    return name


def stage_add_type_hints(
    selection: Selection, workspace: str
) -> QueryResult | None:
    """Add basic type hints (-> None, arg: Any) to the selected function.

    Uses tree-sitter Python grammar to parse the function definition
    and insert missing type annotations. Deterministic, no LLM.
    """
    sel_file = selection.file
    sel_line = selection.line
    if not sel_file or not sel_line:
        return None

    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except Exception:
        return None  # tree-sitter unavailable

    file_path = Path(workspace).resolve() / sel_file
    try:
        original_bytes = file_path.read_bytes()
    except (OSError, UnicodeDecodeError):
        return None

    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(original_bytes)

    # Find the function_definition node that contains the target line.
    # Convert line number to approximate byte offset.
    lines = original_bytes.split(b"\n")
    if sel_line < 1 or sel_line > len(lines):
        return None
    target_byte = sum(len(l) + 1 for l in lines[:sel_line - 1])

    func_node = _find_function_at(tree.root_node, target_byte, lines)
    if func_node is None:
        return None

    # Collect replacements: (start_byte, old_bytes, new_bytes).
    # Apply bottom-up (reverse sort) to preserve byte offsets.
    repls: list[tuple[int, int, bytes]] = []

    # 1. Parameters: add ': Any' after each bare identifier param.
    params_node = func_node.child_by_field_name("parameters")
    if params_node is not None:
        for child in params_node.children:
            if child.type == "identifier":
                # Check if this parameter already has a type annotation.
                # In tree-sitter Python's CST, the next *named* sibling
                # after an identifier param is the 'type' node.
                # We check next_named_sibling (not next_sibling) because
                # the immediate next sibling is the ':' anonymous node.
                nns = child.next_named_sibling
                has_type = nns is not None and nns.type == "type"
                if not has_type:
                    end = child.end_byte
                    repls.append((end, b"", b": Any"))

    # 2. Return type: add ' -> None' before the colon if missing.
    return_type = func_node.child_by_field_name("return_type")
    if return_type is None:
        # Find the closing paren of parameters and insert before the colon.
        if params_node is not None:
            # The colon is right after the parameters.
            colon_pos = params_node.end_byte
            # Skip whitespace to find actual colon position.
            i = colon_pos
            while i < len(original_bytes) and original_bytes[i:i + 1] in (b" ", b"\t"):
                i += 1
            if i < len(original_bytes) and original_bytes[i:i + 1] == b":":
                repls.append((i, b"", b" -> None"))

    if not repls:
        return None  # nothing to add

    # Apply bottom-up.
    repls.sort(key=lambda r: r[0], reverse=True)
    result = bytearray(original_bytes)
    for pos, old, new in repls:
        result[pos:pos + len(old)] = new

    formatted = result.decode("utf-8")
    return _stage_single_file_edit(
        str(file_path), original_bytes.decode("utf-8"), formatted,
        intent="add_type_hints",
    )


def _find_function_at(
    node, target_byte: int, lines: list[bytes],
) -> "Node | None":  # type: ignore[name-defined]
    """Find the innermost function_definition node containing target_byte.

    Searches children first so nested functions are found before their
    enclosing parents. The ``lines`` parameter is unused — kept for API
    consistency with potential future line-based filters.
    """
    _ = lines  # ponytail: unused parameter, kept for API stability
    # Search children first — nested functions take priority.
    for child in node.children:
        result = _find_function_at(child, target_byte, lines)
        if result is not None:
            return result
    # Then check current node.
    if node.type == "function_definition":
        if node.start_byte <= target_byte <= node.end_byte:
            return node
    return None
