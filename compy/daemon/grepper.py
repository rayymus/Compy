"""Step-2 grepper.

Two real options here:
  - `RipgrepGrepper` — wraps `rg --json`. Per spec §5c, this is the v1 baseline because it
    reflects disk state including uncommitted edits (no staleness).
  - `StubGrepper` — deterministic canned hits for tests.

The orchestrator depends only on the Grepper Protocol; swap by construction.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from .interfaces import ReasonerUnavailable
from .models import GrepHit

# Lines that are purely comments — skip these so results don't surface
# documentation strings, inline remarks, or commented-out code as signal.
_COMMENT_ONLY_RE = re.compile(
    r"^\s*(?:#|//|/\*|\* |\*\*/|\"\"\"|'''|--|;|%)"
)


class RipgrepGrepper:
    """Real ripgrep adapter. `rg --json` emits one JSON object per line of output."""

    # Denylist globs — exclude non-code and documentation files.
    # This list blocks:
    #   - Documentation files (.md, .rst, .adoc) — README.md, CHANGELOG, etc.
    #     Users searching for "where is handleRequest" want code, not docs.
    #   - Compiled/minified artifacts (.min.js, .map, .pyc, .class, .o, .so, .dylib)
    #   - Lock files (package-lock.json, yarn.lock, etc.)
    #   - Media & fonts (.png, .jpg, .gif, .ttf, .woff, .pdf, .ico, .svg)
    #   - Logs & data dumps (.log, .csv, .tsv)
    #   - Package directories (node_modules, __pycache__, .git)
    #   - Generated protobuf/bindings (.pb.go, .pb.cc, .d.ts)
    #   - Checksum files (.sum)
    # Everything else passes through — all languages, all configs.
    _SKIP_GLOBS: tuple[str, ...] = (
        "!*.md", "!*.rst", "!*.adoc",           # documentation
        "!*.min.js", "!*.min.css", "!*.min.js.map", "!*.min.css.map",
        "!*.map",
        "!*.pyc", "!*.pyo", "!*.class", "!*.o", "!*.obj", "!*.so", "!*.dylib",
        "!*.pb.go", "!*.pb.cc", "!*.d.ts", "!*.d.mts", "!*.d.cts",
        "!package-lock.json", "!yarn.lock", "!pnpm-lock.yaml", "!Cargo.lock",
        "!Gemfile.lock", "!poetry.lock", "!Pipfile.lock",
        "!*.lock",
        "!*.log", "!*.csv", "!*.tsv",
        "!*.png", "!*.jpg", "!*.jpeg", "!*.gif", "!*.ico", "!*.webp", "!*.svg",
        "!*.pdf", "!*.ttf", "!*.woff", "!*.woff2", "!*.eot",
        "!*.sum",
        "!__pycache__/*", "!node_modules/*", "!.git/*",
    )

    def __init__(self, rg_path: str = "rg", max_results: int = 50) -> None:
        self._rg = rg_path
        self._cap = max_results

    def grep(self, pattern: str, workspace_root: str) -> tuple[GrepHit, ...]:
        # Build -g globs to exclude non-code files (denylist).
        # COMPY_FILE_GLOBS env var lets power users override with a custom list
        # of -g patterns (space-separated). Set to "!*.txt" to add, or empty to disable.
        import os
        custom = os.environ.get("COMPY_FILE_GLOBS", "")
        if custom:
            glob_args = custom.split()
        else:
            glob_args = []
            for ext in self._SKIP_GLOBS:
                glob_args.extend(("-g", ext))
        try:
            proc = subprocess.run(
                [
                    self._rg, "--json", "--no-heading", "--line-number",
                    "--max-count", str(self._cap),
                    "--context", "1",  # 1 line of context before/after each match
                    *glob_args,
                    "-e", pattern, workspace_root,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ReasonerUnavailable(f"ripgrep not found at '{self._rg}'") from exc
        except subprocess.TimeoutExpired as exc:
            raise ReasonerUnavailable(f"ripgrep timed out after {exc.timeout}s") from exc

        # rg exit codes: 0 = matches, 1 = no matches, 2 = error. We treat 0/1 as OK.
        if proc.returncode not in (0, 1):
            raise ReasonerUnavailable(
                f"ripgrep failed (exit {proc.returncode}): {proc.stderr.strip()[:160]}"
            )
        hits = self._parse(proc.stdout)
        return hits[:self._cap]

    def _parse(self, stdout: str) -> tuple[GrepHit, ...]:
        hits: list[GrepHit] = []
        # Collect context lines keyed by (file, line) so we can prepend/append
        # them to match snippets.
        context_before: dict[tuple[str, int], str] = {}  # (file, match_line) -> context text
        context_after: dict[tuple[str, int], str] = {}
        pending_context: str | None = None  # last context line seen

        for line in stdout.splitlines():
            try:
                obj: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            obj_type = obj.get("type")
            if obj_type == "context":
                ctx_data = obj["data"]
                ctx_path = ctx_data["path"]["text"]
                ctx_ln = ctx_data["line_number"]
                ctx_text = (ctx_data.get("lines") or {}).get("text", "").rstrip("\n")
                pending_context = (ctx_path, ctx_ln, ctx_text)
                continue
            if obj_type != "match":
                pending_context = None
                continue
            data = obj["data"]
            path = data["path"]["text"]
            ln = data["line_number"]
            subs = data.get("submatches") or []
            col = subs[0]["start"] if subs else 0
            snippet = (data.get("lines") or {}).get("text", "").rstrip("\n")
            # Skip comment-only lines — they're noise, not code.
            if _COMMENT_ONLY_RE.match(snippet):
                pending_context = None
                continue
            # Prepend context line if available (appears just before this match).
            if pending_context is not None:
                ctx_path2, ctx_ln2, ctx_text2 = pending_context
                if ctx_path2 == path and ctx_ln2 == ln - 1:
                    snippet = ctx_text2[:150] + "\n" + snippet
            hits.append(GrepHit(file=path, line=ln, column=col, snippet=snippet[:400]))
            pending_context = None
        return tuple(hits)


class StubGrepper:
    """Deterministic grepper — feed canned hits and a substring filter.

    Used in tests where we want predictable pipeline state. The `filter` predicate decides
    which canned hits match a given pattern (matched against the snippet text).
    """

    def __init__(self, hits: tuple[GrepHit, ...] = ()) -> None:
        self._hits = hits
        self.calls: list[tuple[str, str]] = []

    def grep(self, pattern: str, workspace_root: str) -> tuple[GrepHit, ...]:
        self.calls.append((pattern, workspace_root))
        return tuple(h for h in self._hits if pattern in h.snippet)
