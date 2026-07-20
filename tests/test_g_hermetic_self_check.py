"""C8-A / G-HERMETIC self-check: proves the REAL entrypoint — the
`sys.addaudithook`-based file-open auditor installed by `tests/conftest.py`'s
`_hermetic_guard` autouse fixture — genuinely detects AND fails an escaping
open, not merely that the pure `gate_hermetic()` function classifies a
hand-built `touches` list correctly (that unit coverage lives in
`tests/test_c8_gates.py`).

Uses a SYNTHETIC covered root (registered via the `hermetic_injection`
fixture) rather than a real machine path (`~/Claude-HQ`, `~/job-opportunity-
radar`) so this test is itself fully hermetic and deterministic — it never
depends on what happens to be present on the machine running it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import conftest  # noqa: E402  (the module under test — pytest also auto-loads it as a plugin)


def test_hermetic_red_synthetic_covered_root_escape_is_flagged(tmp_path, hermetic_injection):
    covered = tmp_path / "synthetic_external_root"
    covered.mkdir()
    target = covered / "secret.txt"
    target.write_text("real, uninjected content\n")

    hermetic_injection.cover("synthetic_c8a_selfcheck", covered)

    with pytest.raises(conftest.HermeticViolation, match="synthetic_c8a_selfcheck"):
        target.read_text()


def test_hermetic_green_injected_substitute_is_allowed(tmp_path, hermetic_injection):
    covered = tmp_path / "synthetic_external_root_2"
    covered.mkdir()
    target = covered / "ok.txt"
    target.write_text("injected substitute content\n")

    hermetic_injection.cover("synthetic_c8a_selfcheck_2", covered)
    hermetic_injection.inject("synthetic_c8a_selfcheck_2", covered)

    # Must NOT raise — the covered root has a registered injected substitute
    # that the resolved path falls under.
    assert target.read_text() == "injected substitute content\n"


def test_hermetic_write_to_synthetic_root_is_also_flagged(tmp_path, hermetic_injection):
    covered = tmp_path / "synthetic_external_root_3"
    covered.mkdir()
    target = covered / "would_write.txt"

    hermetic_injection.cover("synthetic_c8a_selfcheck_3", covered)

    with pytest.raises(conftest.HermeticViolation):
        target.write_text("must never land here uninjected\n")


def test_hermetic_covers_the_real_sensitive_roots_named_by_the_contract():
    # Sanity check only — never actually opens these real paths (that would
    # defeat the point); confirms the fixed covered-root registry matches
    # JOAO_C8_GATE_CONTRACTS.md's "COUVERT" list without touching real data.
    assert conftest._HERMETIC_COVERED_ROOTS["real_memory"] == conftest.REAL_MEMORY_DIR
    assert conftest._HERMETIC_COVERED_ROOTS["external_ledger"] == Path.home() / "Claude-HQ"
    assert conftest._HERMETIC_COVERED_ROOTS["sibling_repos"] == Path.home() / "job-opportunity-radar"
    assert any(name.startswith("credentials:") for name in conftest._HERMETIC_COVERED_ROOTS)


def test_hermetic_pre_existing_exception_allowlist_is_narrow_and_documented():
    # This gate's ONE documented, out-of-C8-A-scope exception — regresses if
    # this ever silently grows without an equally explicit justification.
    assert conftest._HERMETIC_PRE_EXISTING_EXCEPTIONS == {
        "tests/test_b28_import_ledger.py::test_import_is_append_only_idempotent",
    }


def test_hermetic_relative_paths_are_never_classified_against_cwd(tmp_path, monkeypatch):
    # Regression guard: `shutil.rmtree`'s fd-safe walker (and other low-level
    # os.open(name, ..., dir_fd=parent_fd) callers) pass a bare relative NAME
    # that is scoped by `dir_fd`, never by the process cwd — the "open" audit
    # event never carries `dir_fd`. Resolving such a name against cwd
    # previously produced a false positive (an unrelated tmp-dir entry
    # coincidentally named "memory" resolved to the real repo memory/ root).
    # A relative argument must therefore never be classified at all.
    monkeypatch.chdir(conftest.REPO_ROOT)
    touches_before = len(conftest._HERMETIC_TOUCHES)
    conftest._hermetic_audit_hook("open", ("memory", "rb", 0))  # relative — must be a no-op
    assert len(conftest._HERMETIC_TOUCHES) == touches_before


def test_hermetic_real_memory_write_open_is_write_classified():
    assert conftest._hermetic_is_write_open(("p", "r", None)) is False
    assert conftest._hermetic_is_write_open(("p", "rb", None)) is False
    assert conftest._hermetic_is_write_open(("p", "w", None)) is True
    assert conftest._hermetic_is_write_open(("p", "a", None)) is True
    assert conftest._hermetic_is_write_open(("p", None, __import__("os").O_WRONLY)) is True
    assert conftest._hermetic_is_write_open(("p", None, __import__("os").O_RDONLY)) is False


def test_hermetic_ignores_non_open_events():
    touches_before = len(conftest._HERMETIC_TOUCHES)
    conftest._hermetic_audit_hook("os.system", ("echo hi",))
    assert len(conftest._HERMETIC_TOUCHES) == touches_before


def test_hermetic_ignores_already_open_fd_argument():
    touches_before = len(conftest._HERMETIC_TOUCHES)
    conftest._hermetic_audit_hook("open", (3, "r", None))
    assert len(conftest._HERMETIC_TOUCHES) == touches_before
