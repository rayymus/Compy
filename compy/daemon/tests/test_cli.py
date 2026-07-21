"""Tests for the CLI's stdin->stdout JSON contract.

Uses subprocess against `python -m compy.daemon` with `--reasoner stub` so the test never
touches freebuff or ollama.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]  # …/Compy/compy/daemon/tests/test_cli.py → …/Compy/


@pytest.mark.skipif(
    subprocess.run([sys.executable, "-c", "import compy"], capture_output=True).returncode != 0,
    reason="compy package not importable in current env",
)
def test_cli_roundtrip_zero_hits_demotes_to_fuzzy(tmp_path: Path):
    requests_json = json.dumps({
        "question": "where else is get_ability used?",
        "selection": {"text": "def get_ability(self): pass", "file": "/x.py", "line": 1, "workspace_root": str(tmp_path)},
    })
    proc = subprocess.run(
        [sys.executable, "-m", "compy.daemon", "--reasoner", "stub"],
        input=requests_json, capture_output=True, text=True, check=False,
        cwd=str(PROJECT_ROOT),
    )
    assert proc.returncode == 0, f"stderr=\n{proc.stderr}"
    # NDJSON framing: stdout is {"type": "result", "payload": {...}}
    envelope = json.loads(proc.stdout)
    assert envelope["type"] == "result"
    out = envelope["payload"]
    # Spec §2a: empty workspace yields zero grep hits on the structured path, which the
    # orchestrator demotes to the fuzzy branch. With the question's only keyword being
    # "ability" / "used" (and no matching files in tmp_path either), the fuzzy branch
    # produces no candidates and the result is intent="fuzzy", reason="no hits".
    assert out["intent"] == "fuzzy"
    assert out["degraded"] is False
    assert out["hits"] == []
    assert out["reason"] == "no hits"


def test_cli_reports_invalid_input():
    proc = subprocess.run(
        [sys.executable, "-m", "compy.daemon", "--reasoner", "stub"],
        input="not json", capture_output=True, text=True, check=False,
        cwd=str(PROJECT_ROOT),
    )
    assert proc.returncode == 2
    assert "error" in proc.stderr.lower()
