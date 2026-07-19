"""A0.2 §7 (master contract §12.7, run card #7): test memory isolation.

Before this fixture, `RunRuntime`'s lazy memory-subsystem loader
(`_memory_dir()` in `bubble/runtime.py`) defaulted to the REAL, committed
`memory/` directory whenever `JOAO_MEMORY_DIR` was unset — which is every
test that never set it explicitly. Nothing in that path itself corrupted
`memory/lessons.jsonl` during ordinary test runs (the runtime's own retro
hook never calls the append path), but `tests/test_b28_import_ledger.py`'s
`test_import_is_append_only_idempotent` DID run `memory/import_ledger.py` as
a subprocess with no arguments — which reads `~/Claude-HQ/DEFECTS_LEDGER.md`
(external, outside this repo and outside any test's control) and writes
straight into the real, committed `memory/lessons.jsonl` if that external
ledger had changed since the last import. That is the actual mechanism
behind the "2-3 flaky tests depending on `memory/lessons.jsonl`'s current
content" flagged in the A0/A0.1 reports.

This autouse, session-scoped fixture makes the whole suite hermetic: it
copies the memory subsystem's code + its `lessons.jsonl`/`run_metrics.jsonl`
stock into an isolated temp directory once, then points `JOAO_MEMORY_DIR` at
that copy for the duration of the test session. Every `RunRuntime` created
during a test resolves memory injection/retro/ledger-sync against the
isolated copy — the real repo `memory/` directory is never opened for a
write by anything going through `_memory_dir()`. Tests that reach the memory
modules directly via their own `sys.path` manipulation are unaffected in
behavior (same code, byte-identical copy) and remain independently hermetic
via their own `tmp_path`/`monkeypatch` fixtures (see test_b28_retro.py).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_MEMORY_DIR = REPO_ROOT / "memory"


@pytest.fixture(autouse=True, scope="session")
def _isolated_joao_memory_dir(tmp_path_factory):
    # `select_lessons.py` (and, transitively, `import_ledger.py`) resolve
    # their own `LESSONS` constant as `Path(__file__).resolve().parents[1] /
    # "memory" / "lessons.jsonl"` — i.e. they assume they live at
    # `<repo_root>/memory/<file>.py`, not merely "next to lessons.jsonl". A
    # flat copy (files dropped directly into a tmp dir) breaks that
    # assumption silently (load_lessons() returns [] with no error). The
    # isolated copy must therefore mirror the real layout: a fake repo root
    # containing its own `memory/` subdirectory.
    fake_repo_root = tmp_path_factory.mktemp("joao-memory-isolated")
    isolated = fake_repo_root / "memory"
    isolated.mkdir()
    if REAL_MEMORY_DIR.is_dir():
        for item in REAL_MEMORY_DIR.iterdir():
            if item.name == "__pycache__":
                continue
            if item.is_file():
                shutil.copy2(item, isolated / item.name)
    previous = os.environ.get("JOAO_MEMORY_DIR")
    os.environ["JOAO_MEMORY_DIR"] = str(isolated)
    try:
        yield isolated
    finally:
        if previous is None:
            os.environ.pop("JOAO_MEMORY_DIR", None)
        else:
            os.environ["JOAO_MEMORY_DIR"] = previous
