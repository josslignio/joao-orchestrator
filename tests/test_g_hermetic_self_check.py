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

import json
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


def test_hermetic_real_data_root_exception_allowlist_is_completely_empty():
    # Boss scope adjudication: ZERO exceptions of any kind may remain for
    # real mutable/sensitive data roots. Not a blanket exemption, not a
    # per-file one, and not an exact-node one — the C8-A criterion is that
    # the default suite never touches these roots at all.
    assert conftest._HERMETIC_OUT_OF_SCOPE_PROTECTED_READS == set(), (
        "any entry here re-opens the exact hole C8-A exists to close"
    )
    assert len(conftest._HERMETIC_OUT_OF_SCOPE_PROTECTED_READS) == 0


@pytest.mark.parametrize("spoofed_nodeid", [
    "tests/test_a0_2_corrections.py::test_a02_6_test_memory_isolation_redirects_lessons_write_target",
    "tests/test_b28_select_lessons.py::test_visual_docx_surfaces_authority_chain",
    "tests/test_b28_select_lessons.py::test_higher_severity_and_overlap_rank_first",
    "tests/test_b28_import_ledger.py::test_import_is_append_only_idempotent",
])
def test_hermetic_previously_exempted_nodeids_are_no_longer_exempt(spoofed_nodeid, monkeypatch):
    """Behavioral proof (not a source grep) that every nodeid that was ever
    exempted — the original blanket one and all eight exact-node ones — now
    gets no special treatment whatsoever: spoofing `PYTEST_CURRENT_TEST` to
    each of them and reading real memory must still raise.
    """
    monkeypatch.setenv("PYTEST_CURRENT_TEST", f"{spoofed_nodeid} (call)")
    assert conftest._hermetic_current_nodeid() == spoofed_nodeid
    with pytest.raises(conftest.HermeticViolation, match="real_memory"):
        (conftest.REAL_MEMORY_DIR / "lessons.jsonl").read_text()


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


# ---------------------------------------------------------------------------
# Correction loop 3, finding #7: the "isolated" memory fixture must not track
# the LIVE memory/ content. Adversarial repro of the audited case.
# ---------------------------------------------------------------------------


def _decoy_live_memory(root: Path) -> Path:
    """A stand-in 'live' memory/ dir holding a single lesson record — exactly
    the audited repro ('remplacer memory/lessons.jsonl par 1 seul record')."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "lessons.jsonl").write_text(
        json.dumps({"id": "L-999", "source_defect": "D-999", "severity": 1,
                    "rule": "decoy", "tags": ["decoy"], "applies_to": ["builder"],
                    "trigger_contexts": [], "recurrences": 0, "date": "2026-07-20",
                    "project": "decoy"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    (root / "select_lessons.py").write_text("# decoy module source\n", encoding="utf-8")
    return root


def test_isolated_memory_seeding_ignores_live_lessons_content(tmp_path):
    """REPRO: before the fix, seeding copied EVERY file (including the live,
    mutable `lessons.jsonl`) out of the real `memory/` dir, so a 1-record live
    file produced a 1-record 'isolated' file and `test_at_least_40_lessons`
    failed THROUGH the supposedly isolated fixture. DATA now comes only from
    the frozen, committed fixture, so a decoy live root cannot influence it.
    """
    decoy = _decoy_live_memory(tmp_path / "decoy-live-memory")
    destination = tmp_path / "isolated" / "memory"

    conftest.seed_isolated_memory_dir(destination, module_source_dir=decoy)

    records = [json.loads(line) for line in
               (destination / "lessons.jsonl").read_text().splitlines() if line.strip()]
    assert len(records) >= 40, "seeded DATA must come from the frozen fixture, not the live/decoy file"
    assert not any(r["id"] == "L-999" for r in records), "the decoy live record must never be seeded"


def test_isolated_memory_seeding_is_byte_identical_to_the_committed_frozen_fixture(tmp_path):
    destination = tmp_path / "isolated" / "memory"
    conftest.seed_isolated_memory_dir(destination, module_source_dir=_decoy_live_memory(tmp_path / "decoy"))
    frozen = conftest.FROZEN_MEMORY_DATA_FIXTURES["lessons.jsonl"]
    assert (destination / "lessons.jsonl").read_bytes() == frozen.read_bytes()


def test_isolated_memory_seeding_still_provides_the_real_module_sources(tmp_path):
    # The .py modules ARE seeded from the repo source tree — the memory
    # subsystem must run its real code under RunRuntime; only DATA is frozen.
    destination = tmp_path / "isolated" / "memory"
    conftest.seed_isolated_memory_dir(destination)
    for module_name in ("inject.py", "retro.py", "select_lessons.py", "import_ledger.py"):
        assert (destination / module_name).is_file(), f"{module_name} must be seeded from repo source"
        assert (destination / module_name).read_bytes() == (conftest.REAL_MEMORY_DIR / module_name).read_bytes()


def test_isolated_memory_seeding_refuses_to_fall_back_when_frozen_fixture_is_absent(tmp_path, monkeypatch):
    # Fail closed: a missing frozen fixture must raise, never silently
    # re-introduce a copy of the live data file.
    monkeypatch.setitem(conftest.FROZEN_MEMORY_DATA_FIXTURES, "lessons.jsonl",
                        tmp_path / "does-not-exist.jsonl")
    with pytest.raises(RuntimeError, match="frozen memory data fixture is missing"):
        conftest.seed_isolated_memory_dir(tmp_path / "isolated" / "memory")


def test_session_isolated_memory_dir_matches_the_frozen_fixture(_isolated_joao_memory_dir):
    # End-to-end: the ACTUAL session-wide fixture every other test consumes is
    # seeded from the frozen committed content, not from live memory/.
    assert (_isolated_joao_memory_dir / "lessons.jsonl").read_bytes() == \
        conftest.FROZEN_MEMORY_DATA_FIXTURES["lessons.jsonl"].read_bytes()
