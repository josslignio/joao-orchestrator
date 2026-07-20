"""C8-A / G-HERMETIC self-check: proves the REAL entrypoint — the
`sys.addaudithook`-based file-open auditor installed by `tests/conftest.py`'s
`_hermetic_guard` autouse fixture — genuinely detects AND fails an escaping
open, not merely that the pure `gate_hermetic()` function classifies a
hand-built `touches` list correctly (that unit coverage lives in
`tests/test_c8_gates.py`).

Correction loop (post first-review CHANGES_REQUIRED): adds the explicit
adversarial probes demanded — direct real-memory read AND write, direct
real-external-ledger read, a relative-path escape into a covered real root,
and the injected-substitute green case — on top of the pre-existing
synthetic-root self-check (kept, since it proves the mechanism without
depending on any real machine path for the bulk of the coverage).
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


def test_hermetic_out_of_scope_protected_reads_allowlist_is_narrow_read_only_and_documented():
    # Correction loop: the OLD blanket test-node exemption is gone. What
    # remains is a distinct, minimal, per-nodeid allowlist for tests this
    # correction is contractually forbidden to modify (an A0.2 test, and a
    # file outside this correction's ALLOWED FILES) — never a write escape
    # hatch (`_hermetic_is_write_open` unconditionally overrides it), and
    # never applicable to this file's own nodeids.
    allowlist = conftest._HERMETIC_OUT_OF_SCOPE_PROTECTED_READS
    assert allowlist == {
        "tests/test_a0_2_corrections.py::test_a02_6_test_memory_isolation_redirects_lessons_write_target",
        "tests/test_b28_select_lessons.py::test_visual_docx_surfaces_authority_chain",
        "tests/test_b28_select_lessons.py::test_async_mission_surfaces_the_async_race_lesson",
        "tests/test_b28_select_lessons.py::test_no_tags_falls_back_to_systemic_severity_3",
        "tests/test_b28_select_lessons.py::test_token_budget_is_respected",
        "tests/test_b28_select_lessons.py::test_files_touched_drives_tag_inference",
        "tests/test_b28_select_lessons.py::test_determinism_repeated_10x",
        "tests/test_b28_select_lessons.py::test_higher_severity_and_overlap_rank_first",
    }
    assert not any("test_g_hermetic_self_check" in nodeid for nodeid in allowlist)
    assert not any("test_b28_import_ledger" in nodeid for nodeid in allowlist), (
        "the ORIGINAL blanket exemption (a fixable test in an authorized file) must stay removed"
    )


def test_hermetic_relative_paths_are_resolved_against_cwd_not_skipped(tmp_path, monkeypatch):
    # Correction loop: a relative path under a high-level open() call (a
    # string mode, e.g. 'r'/'w' — never the low-level os.open() shape) must
    # be resolved against the effective cwd and checked like any absolute
    # path — it must NOT bypass the auditor merely by being relative.
    covered = tmp_path / "synthetic_relative_root"
    covered.mkdir()
    (covered / "target.txt").write_text("x")
    monkeypatch.chdir(covered)

    touches_before = len(conftest._HERMETIC_TOUCHES)
    conftest._HERMETIC_EXTRA_COVERED["synthetic_c8a_relative_check"] = covered
    try:
        with pytest.raises(conftest.HermeticViolation, match="synthetic_c8a_relative_check"):
            open("target.txt", "r").close()  # noqa: SIM115 — deliberately bare, relative path
    finally:
        conftest._HERMETIC_EXTRA_COVERED.pop("synthetic_c8a_relative_check", None)
    assert len(conftest._HERMETIC_TOUCHES) > touches_before


def test_hermetic_low_level_os_open_dir_fd_relative_call_is_never_misresolved_against_cwd(tmp_path, monkeypatch):
    # Regression guard: `shutil.rmtree`'s fd-safe walker (and other low-level
    # os.open(name, ..., dir_fd=parent_fd) callers) pass a bare relative NAME
    # that is scoped by `dir_fd`, never by the process cwd — the "open" audit
    # event never carries `dir_fd`. Resolving such a name against cwd
    # previously produced a false positive (an unrelated tmp-dir entry
    # coincidentally named "memory" resolved to the real repo memory/ root).
    # A relative argument under a NON-string mode (os.open()'s shape) must
    # therefore never be classified at all, cwd or not.
    monkeypatch.chdir(conftest.REPO_ROOT)
    touches_before = len(conftest._HERMETIC_TOUCHES)
    conftest._hermetic_audit_hook("open", ("memory", None, 16777220))  # os.open()-shaped: mode=None
    assert len(conftest._HERMETIC_TOUCHES) == touches_before


# ---------------------------------------------------------------------------
# Correction loop: mandatory real-root adversarial probes.
# ---------------------------------------------------------------------------


def test_hermetic_direct_read_of_real_memory_blocks():
    target = conftest.REAL_MEMORY_DIR / "lessons.jsonl"
    with pytest.raises(conftest.HermeticViolation, match="real_memory"):
        target.read_text()


def test_hermetic_direct_write_of_real_memory_blocks():
    target = conftest.REAL_MEMORY_DIR / "c8a_correction_write_probe.tmp"
    assert not target.exists(), "probe target must not already exist — this test proves it never gets created"
    with pytest.raises(conftest.HermeticViolation, match="real_memory"):
        target.write_text("must never actually land on disk\n")
    assert not target.exists(), "the write must never have reached disk — the auditor raises before the OS open"


def test_hermetic_direct_read_of_real_external_ledger_blocks():
    # Deterministic regardless of whether ~/Claude-HQ/DEFECTS_LEDGER.md
    # exists on this machine — the audit hook fires (and raises) before the
    # underlying filesystem open is ever attempted (verified: it fires even
    # for a nonexistent path, raising before FileNotFoundError would).
    target = Path.home() / "Claude-HQ" / "DEFECTS_LEDGER.md"
    with pytest.raises(conftest.HermeticViolation, match="external_ledger"):
        target.read_text()


def test_hermetic_relative_path_access_into_a_covered_real_root_blocks(monkeypatch):
    monkeypatch.chdir(conftest.REAL_MEMORY_DIR)
    with pytest.raises(conftest.HermeticViolation, match="real_memory"):
        open("lessons.jsonl", "r").close()  # noqa: SIM115 — deliberately bare, relative path


def test_hermetic_injected_tmp_path_equivalent_of_real_memory_passes(tmp_path, _isolated_joao_memory_dir):
    # The green mirror of the two red real_memory probes above: the SAME
    # logical file, read through the already-injected isolated substitute
    # every RunRuntime-mediated test uses, passes cleanly.
    isolated_target = _isolated_joao_memory_dir / "lessons.jsonl"
    assert isolated_target.is_file()
    isolated_target.read_text()  # must not raise


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
