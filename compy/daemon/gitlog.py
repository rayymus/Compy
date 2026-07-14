"""Git history querier — "why was this changed" queries.

Queries git log and git blame for historical context on code. Results are surfaced
as GrepHit tuples so they flow through the existing reasoner chain without changes.

Query methods:
  - query_history(question, workspace)     → git log --all --oneline --grep
  - query_blame(file, line, workspace)     → git blame -L on a specific line
  - query_file_history(file, workspace)    → git log --oneline for a specific file

Per SPEC §8f: this answers a different question class (historical intent) from
structural search — it feeds commit messages and diffs to the reasoner for ranking.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .interfaces import ReasonerUnavailable
from .models import GrepHit

_GIT = "git"


def _run_git(args: list[str], cwd: str | None = None, timeout: float = 20.0) -> str:
    try:
        proc = subprocess.run(
            [_GIT] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ReasonerUnavailable(f"git not found at '{_GIT}'") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReasonerUnavailable(f"git log timed out after {exc.timeout}s") from exc
    if proc.returncode not in (0, 1):
        # 0 = success, 1 = no results (not an error for grep-style queries)
        raise ReasonerUnavailable(
            f"git failed (exit {proc.returncode}): {proc.stderr.strip()[:160]}"
        )
    return proc.stdout


class GitHistory:
    """Queries git history and formats results as GrepHits for the reasoner chain."""

    def query_history(self, question: str, workspace_root: str) -> tuple[GrepHit, ...]:
        """Search git log for commits matching keywords from the question."""
        repo = self._find_repo(workspace_root)
        if repo is None:
            return ()

        # Extract meaningful keywords from the question.
        keywords = [w for w in question.lower().split() if len(w) > 2 and w not in {
            "the", "why", "was", "who", "what", "when", "added", "changed",
            "this", "that", "did", "does", "find", "show", "tell",
        }]

        if not keywords:
            return ()

        hits: list[GrepHit] = []
        for kw in keywords[:5]:  # cap at 5 keywords to avoid slow greps
            try:
                out = _run_git(
                    ["log", "--all", "--oneline", "--grep", kw, "-n", "10"],
                    cwd=repo,
                )
            except ReasonerUnavailable:
                continue
            for line in out.strip().splitlines():
                if line.strip():
                    parts = line.split(" ", 1)
                    commit_hash = parts[0] if parts else ""
                    message = parts[1] if len(parts) > 1 else ""
                    hits.append(GrepHit(
                        file=f"git:commit:{commit_hash[:8]}",
                        line=0,
                        column=0,
                        snippet=message[:300],
                        symbol=kw,
                    ))
        return tuple(hits)

    def query_blame(self, file: str, line: int, workspace_root: str) -> tuple[GrepHit, ...]:
        """Blame a specific file:line to find who last changed it."""
        repo = self._find_repo(workspace_root)
        if repo is None or not file:
            return ()
        full_path = Path(repo) / file
        if not full_path.exists():
            return ()

        try:
            out = _run_git(
                ["blame", "-L", f"{line},{line}", "--", str(full_path)],
                cwd=repo,
            )
        except ReasonerUnavailable:
            return ()

        hits: list[GrepHit] = []
        for bl_line in out.strip().splitlines():
            if bl_line.strip():
                # git blame format: <hash> (<author> <date> <line>) <content>
                parts = bl_line.split("(", 1)
                commit_hash = parts[0].strip()[:8] if parts else ""
                rest = parts[1] if len(parts) > 1 else ""
                hits.append(GrepHit(
                    file=f"git:commit:{commit_hash}",
                    line=line,
                    column=0,
                    snippet=f"blame: {rest[:280]}" if rest else bl_line[:300],
                ))
        return tuple(hits)

    def query_file_history(self, file: str, workspace_root: str) -> tuple[GrepHit, ...]:
        """Show recent commits touching a specific file."""
        repo = self._find_repo(workspace_root)
        if repo is None or not file:
            return ()
        full_path = Path(repo) / file
        if not full_path.exists():
            return ()

        try:
            out = _run_git(
                ["log", "--oneline", "-n", "10", "--", str(full_path)],
                cwd=repo,
            )
        except ReasonerUnavailable:
            return ()

        hits: list[GrepHit] = []
        for log_line in out.strip().splitlines():
            if log_line.strip():
                parts = log_line.split(" ", 1)
                commit_hash = parts[0] if parts else ""
                message = parts[1] if len(parts) > 1 else ""
                hits.append(GrepHit(
                    file=f"git:commit:{commit_hash[:8]}",
                    line=0,
                    column=0,
                    snippet=message[:300],
                ))
        return tuple(hits)

    def _find_repo(self, workspace_root: str) -> str | None:
        """Find the git repo root containing workspace_root."""
        path = Path(workspace_root).resolve()
        for parent in [path, *path.parents]:
            if (parent / ".git").is_dir():
                return str(parent)
        return None
