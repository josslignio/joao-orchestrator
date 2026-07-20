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
# Correction loop (post first-review CHANGES_REQUIRED): the previous version
# (a) exempted `real_memory` reads entirely (write-only enforcement) and
# (b) carried a blanket test-node exemption for
# `tests/test_b28_import_ledger.py::test_import_is_append_only_idempotent`.
# Both are removed. `real_memory` is now enforced on every open, read or
# write, exactly like every other covered root — the READ-side leak this
# used to paper over (`memory/*.py`'s own bare `import select_lessons`-style
# cross-imports resolving through a stale `sys.modules` entry bound to
# whichever memory root — real or isolated — happened to import that name
# FIRST in the process) is fixed at its actual source below
# (`_purge_ambiguous_memory_module_cache`), not by carving out the read side
# of the gate. `test_b28_import_ledger.py` itself was rewritten to read the
# already-injected isolated memory snapshot instead of the live real file —
# there is no longer any exemption list at all.
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

# `memory/*.py` modules cross-import each other by bare name (`import
# select_lessons`, `import inject`, ...), relying on whichever directory is
# highest on `sys.path` at import time. `sys.modules` caches by bare name,
# so whichever copy (real vs isolated) wins the FIRST such import for a
# process sticks for every later importer regardless of sys.path — closed
# below by `_purge_ambiguous_memory_module_cache`, never by exempting reads.
_MEMORY_AMBIGUOUS_MODULE_NAMES = ("inject", "retro", "ledger_sync", "select_lessons", "import_ledger")

# Boss scope adjudication (continuation of the same targeted correction):
# this set is now, and must remain, EMPTY. G-HERMETIC's C8-A acceptance
# criterion is that the default suite never reads or writes a real JOÃO
# mutable/sensitive data root — an exact-node read exception is narrower
# than a blanket one but still fails that criterion, so the two tests that
# previously needed entries here were fixed at the source instead:
#   - tests/test_a0_2_corrections.py::test_a02_6_... now snapshots the
#     module's own genuine pre-redirection default target (which resolves
#     inside the injected `JOAO_MEMORY_DIR` copy), not the real committed
#     file. Every A0.2 assertion is preserved; only the data source changed.
#   - tests/test_b28_select_lessons.py no longer binds the production module
#     at collection time; it writes a deterministic frozen lessons fixture
#     under tmp_path first, then imports the REAL selector and parses that
#     frozen file with the real `load_lessons()`. All seven behavioral
#     assertions are preserved; only the data source changed.
# Any future entry here re-opens the exact hole C8-A exists to close. The
# emptiness of this set is itself asserted by
# tests/test_g_hermetic_self_check.py.
_HERMETIC_OUT_OF_SCOPE_PROTECTED_READS: set[str] = set()


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
    """Used ONLY to bound `_HERMETIC_OUT_OF_SCOPE_PROTECTED_READS` to reads —
    every other covered-root check is read/write-symmetric."""
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
    mode = args[1] if len(args) > 1 else None
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        # Correction loop: relative paths must not bypass the auditor — they
        # are resolved against the effective cwd, not skipped outright.
        # EXCEPT: a relative `path`/`name` passed to the low-level
        # `os.open()` alongside `dir_fd` (mode is never a string for
        # `os.open()` — only `io.open()`/`Path.open()`/builtin `open()` ever
        # pass a string mode) is NOT reliably cwd-relative — the "open" audit
        # event never carries `dir_fd` at all, so such a call is ambiguous.
        # Verified empirically: CPython's own `shutil.rmtree` fd-safe walker
        # (used by pytest's own tmp-dir cleanup) calls exactly
        # `os.open(entry_name, flags, dir_fd=parent_fd)` for every directory
        # entry, with `mode=None` — resolving that bare entry name against
        # cwd previously produced a false positive (an unrelated tmp-dir
        # entry coincidentally named "memory"). A relative path under a
        # STRING mode (the high-level open() family) is always genuinely
        # cwd-relative and IS resolved below — this exception is narrowly
        # scoped to the non-string-mode/relative-path combination only.
        if not isinstance(mode, str):
            return
        expanded = os.path.join(os.getcwd(), expanded)
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
    if injected:
        return
    if (not _hermetic_is_write_open(args)
            and _hermetic_current_nodeid() in _HERMETIC_OUT_OF_SCOPE_PROTECTED_READS):
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


@pytest.fixture(autouse=True)
def _purge_ambiguous_memory_module_cache():
    """Correction loop: `real_memory` is now enforced on reads too, which
    surfaced a real, pre-existing cross-test leak — `memory/*.py`'s bare
    `import select_lessons`/`import inject`/etc. cross-imports resolve
    through whichever `sys.path` entry is highest AT IMPORT TIME, but
    `sys.modules` then caches the result by bare name for the rest of the
    process. Whichever copy (the real `memory/` dir, via a test's own direct
    `sys.path.insert`, or the isolated copy, via `RunRuntime`'s
    `_memory_dir()`) happens to import a given bare name FIRST in the whole
    session silently "wins" it for every later importer, regardless of that
    later importer's own `sys.path` state. Purging these specific names from
    `sys.modules` before and after every test forces each bare import to
    re-resolve fresh against whatever `sys.path` is current for that test —
    closing the leak without touching `memory/*.py` itself (out of this
    correction's file scope) and without weakening the hermetic check.
    """
    for name in _MEMORY_AMBIGUOUS_MODULE_NAMES:
        sys.modules.pop(name, None)
    yield
    for name in _MEMORY_AMBIGUOUS_MODULE_NAMES:
        sys.modules.pop(name, None)


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
