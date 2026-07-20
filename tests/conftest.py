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
import sys
import threading
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# C8-A / G-HERMETIC (`JOAO_C8_GATE_CONTRACTS.md`): a real, enforcing file-open
# auditor for the sensitive/mutable data roots the contract names as
# "COUVERT" — real memory data, external ledgers, sibling repos, and
# credential/config directories. Bounded exactly to those roots: repo
# sources, Python/stdlib, installed packages and `.pytest_cache` are never
# classified at all (explicitly out of scope, matching the contract's own
# carve-out) — this is a gate of DETECTION AND FAILURE, not merely a source
# of isolated copies (the fixture above already provides those for memory/).
#
# Mechanism: `sys.addaudithook` on the "open" event fires for every real file
# open in this process (`open()`, `os.open()`, `Path.open()` all funnel
# through it) for the remaining lifetime of the interpreter. A resolved path
# under a covered root that is NOT under a currently-registered injected
# substitute raises `HermeticViolation` from inside the `open()` call itself
# — the escaping test fails immediately, not merely a warning.
#
# The one pre-existing, out-of-C8-A-scope exception (`_HERMETIC_PRE_EXISTING_
# EXCEPTIONS` below) is documented, not silent: without it this new gate
# would regress `tests/test_b28_import_ledger.py::test_import_is_append_only_
# idempotent`, a test outside C8-A's authorized file scope whose read-side
# dependency on the real `~/Claude-HQ/DEFECTS_LEDGER.md` predates this
# milestone (only its WRITE side needed isolation, already handled above).
# ---------------------------------------------------------------------------


class HermeticViolation(RuntimeError):
    """Raised from inside the "open" audit hook the instant a real,
    uninjected path under a G-HERMETIC covered root is opened."""


_HERMETIC_CREDENTIAL_ROOTS = (Path.home() / ".ssh", Path.home() / ".aws")

_HERMETIC_COVERED_ROOTS: dict[str, Path] = {
    "real_memory": REAL_MEMORY_DIR,
    "external_ledger": Path.home() / "Claude-HQ",
    "sibling_repos": Path.home() / "job-opportunity-radar",
}
for _cred_root in _HERMETIC_CREDENTIAL_ROOTS:
    _HERMETIC_COVERED_ROOTS[f"credentials:{_cred_root.name}"] = _cred_root

# Mutable, session-lifetime registries a test can extend for its own duration
# only via the `hermetic_injection` fixture below (never edited directly) —
# lets `tests/test_g_hermetic_self_check.py` prove the red->green mechanism
# without depending on any real machine path.
_HERMETIC_EXTRA_COVERED: dict[str, Path] = {}
_HERMETIC_INJECTED: dict[str, list[Path]] = {}
_HERMETIC_TOUCHES: list[dict[str, Any]] = []
_HERMETIC_LOCK = threading.Lock()

_HERMETIC_PRE_EXISTING_EXCEPTIONS = {
    "tests/test_b28_import_ledger.py::test_import_is_append_only_idempotent",
}


def _hermetic_current_nodeid() -> str:
    return os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]


def _hermetic_classify(resolved: Path) -> str | None:
    """Return the covered-root name `resolved` falls under, or None if it is
    out of G-HERMETIC's explicitly bounded scope (never audited)."""
    roots = {**_HERMETIC_COVERED_ROOTS, **_HERMETIC_EXTRA_COVERED}
    for name, root in roots.items():
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if name == "real_memory" and (resolved.suffix == ".py" or "__pycache__" in resolved.parts):
            continue  # repo source .py files under memory/ stay explicitly allowed
        return name
    return None


def _hermetic_is_injected(resolved: Path, root_name: str) -> bool:
    for substitute in _HERMETIC_INJECTED.get(root_name, []):
        try:
            resolved.relative_to(substitute)
            return True
        except ValueError:
            continue
    return False


def _hermetic_is_write_open(args: tuple) -> bool:
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    if isinstance(flags, int):
        write_bits = getattr(os, "O_WRONLY", 0) | getattr(os, "O_RDWR", 0) | \
            getattr(os, "O_APPEND", 0) | getattr(os, "O_CREAT", 0) | getattr(os, "O_TRUNC", 0)
        if flags & write_bits:
            return True
    return False


def _hermetic_audit_hook(event: str, args: tuple) -> None:
    if event != "open":
        return
    file_arg = args[0]
    if isinstance(file_arg, int):
        return  # already-open fd — nothing to classify
    try:
        raw = os.fspath(file_arg)
        if isinstance(raw, bytes):
            raw = raw.decode(errors="surrogateescape")
    except TypeError:
        return
    # A relative `path`/`name` passed alongside `dir_fd` (as CPython's own
    # `shutil.rmtree` fd-safe walker does for every directory entry during
    # tmp-dir cleanup) is NOT relative to this process's cwd — the "open"
    # audit event never carries `dir_fd`, so a relative argument here is
    # ambiguous and must never be resolved against cwd (doing so previously
    # produced false positives: an unrelated tmp-dir entry named "memory"
    # falsely resolved to the real repo `memory/` root). Only absolute paths
    # (after `~` expansion) are ever classified.
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return
    try:
        resolved = Path(expanded).resolve()
    except (OSError, ValueError):
        return
    root_name = _hermetic_classify(resolved)
    if root_name is None:
        return
    injected = _hermetic_is_injected(resolved, root_name)
    with _HERMETIC_LOCK:
        _HERMETIC_TOUCHES.append({"root": root_name, "path": str(resolved), "injected": injected})
    if injected or _hermetic_current_nodeid() in _HERMETIC_PRE_EXISTING_EXCEPTIONS:
        return
    # `real_memory`: A0.2 §7's own documented threat model is WRITE corruption
    # of the real, committed `memory/lessons.jsonl` (its fixture's docstring:
    # "nothing in that path itself corrupted memory/lessons.jsonl ... the
    # runtime's own retro hook never calls the append path") — a READ of the
    # real, versioned, static data file (e.g. a stdlib module-cache reuse of
    # `select_lessons.py` imported earlier via a different sys.path insertion
    # resolving its own `LESSONS` constant against the real repo root) is not
    # the risk this gate exists to close, and touching that pre-existing
    # cross-test import behavior is outside C8-A's authorized file scope.
    # G-HERMETIC enforces real_memory strictly on WRITES; every other covered
    # root (external ledgers, sibling repos, credentials) is enforced on any
    # open, read or write — there is no legitimate reason to touch those at
    # all under test.
    if root_name == "real_memory" and not _hermetic_is_write_open(args):
        return
    raise HermeticViolation(
        f"G-HERMETIC: uninjected open of covered root {root_name!r} at {resolved} — resolve this "
        "root under tmp_path, or register an injected substitute via the `hermetic_injection` fixture"
    )


@pytest.fixture(autouse=True, scope="session")
def _hermetic_guard(_isolated_joao_memory_dir):
    # Explicitly depends on `_isolated_joao_memory_dir` so pytest instantiates
    # that fixture FIRST — its one-time seeding copy (real memory/ -> isolated
    # tmp copy) legitimately reads the real memory/ directory once; the audit
    # hook must only be installed AFTER that seeding read completes, never
    # racing ahead of it regardless of fixture declaration order.
    # sys.addaudithook cannot be removed once installed (by design) — added
    # exactly once here, for the lifetime of this pytest process.
    sys.addaudithook(_hermetic_audit_hook)
    yield


class HermeticInjectionHandle:
    """Handle returned by the `hermetic_injection` fixture: lets a single
    test register an extra covered root and/or an injected substitute for
    its own duration only — reverted automatically at teardown."""

    def __init__(self) -> None:
        self._added_covered: list[str] = []
        self._added_injected: list[tuple[str, Path]] = []

    def cover(self, name: str, root: Path) -> None:
        _HERMETIC_EXTRA_COVERED[name] = Path(root)
        self._added_covered.append(name)

    def inject(self, name: str, substitute: Path) -> None:
        _HERMETIC_INJECTED.setdefault(name, []).append(Path(substitute))
        self._added_injected.append((name, Path(substitute)))


@pytest.fixture
def hermetic_injection():
    handle = HermeticInjectionHandle()
    try:
        yield handle
    finally:
        for name in handle._added_covered:
            _HERMETIC_EXTRA_COVERED.pop(name, None)
        for name, substitute in handle._added_injected:
            remaining = _HERMETIC_INJECTED.get(name, [])
            if substitute in remaining:
                remaining.remove(substitute)
