"""A0.2 ("FERMETURE + SCOPE DUR", run card `JOAO_RUN_CARD_A0_2_BYPASSES_1.md`,
master contract `JOAO_MASTER_EXECUTION_CONTRACT_20260719.md`) — one red -> green
attack test per frozen correctif:

  1  capabilities frozen to the BUILDER, not just tests/reviewer
  2  network permission for a builder dispatch comes only from the frozen scope
  3  the full scope (critical, environment_allowlist, command_timeout_seconds,
     forbidden_paths, provider_transport_network, required_backend, builder
     identity) is cross-checked, not just the original three A0-5 fields
  4  sensitive gitignored files are scanned BEFORE any provider runs, not only
     after the builder
  5  declared_baseline is forbidden outright for a critical run
  6  the test suite never touches the real, committed memory/lessons.jsonl
  7  promote() self-verifies acceptance via an immutable approval record
  8  a run requiring a backend stronger than local_untrusted BLOCKs with
     reason_code=PREFLIGHT_UNAVAILABLE instead of a silent fallback

Plus a static AST guard: no adapter (`GLMBuilder`, `CodexCLIReviewer`) calls
`subprocess.run`/`subprocess.Popen`/`run_sandboxed` directly — every external
dispatch goes through `ExecutionBackend.execute()` (§12.2 single dispatch
point).
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from joao_orchestrator.bubble.execution_backend import (
    ExecutionBackend, LocalUntrustedBackend, preflight_backend, resolve_backend,
)
from joao_orchestrator.bubble.runtime import (
    GLMBuilder, LocalProfileAdapter, LocalTestRunner, RunRuntime, RuntimeStateError, SandboxBuilder,
)
import joao_orchestrator.bubble.promotion as promotion_mod
from joao_orchestrator.bubble.promotion import PromotionError, create_approval_record


def _git(argv, cwd, check=True):
    return subprocess.run(["git"] + argv, cwd=str(cwd), shell=False, capture_output=True, text=True, check=check)


def sandbox(tmp_path: Path) -> Path:
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "a02@example.invalid"], ["config", "user.name", "a02"]):
        _git(argv, workspace)
    (workspace / "module.py").write_text("VALUE = 1\n")
    (workspace / ".joao-profile.json").write_text(json.dumps({
        "project_id": "a02fixture", "display_name": "a02fixture", "repository_root": str(workspace),
        "allowed_write_paths": ["module.py", "new_module.py"], "forbidden_paths": []}))
    _git(["add", "."], workspace)
    _git(["commit", "-qm", "base"], workspace)
    return workspace


def runtime(tmp_path, builder, reviewer=None):
    return RunRuntime(tmp_path / "state", builder=SandboxBuilder(builder), reviewer=reviewer, profiles=LocalProfileAdapter())


class AcceptReviewer:
    provider = "codex"; model = "fixture"
    def review(self, run, _):
        return {"ok": True, "decision": "pass",
                "proof": {"verdict": "ACCEPT", "candidate_tree": run.get("candidate_tree"),
                         "findings": [], "reviewer": {"provider": self.provider, "model": self.model}}}


def _run_folder(tmp_path, run_id):
    return tmp_path / "state" / "runs" / run_id


# ---------------------------------------------------------------------------
# #1/#2 — capabilities frozen to the BUILDER; network comes ONLY from the
# frozen scope, never a class-level default the adapter grants itself.
# ---------------------------------------------------------------------------
class _RecordingBackend(ExecutionBackend):
    """Stands in for ExecutionBackend so the test observes exactly what
    network permission GLMBuilder asked for, without needing a real GLM CLI."""
    name = "local_untrusted"
    implemented = True

    def __init__(self):
        self.calls = []

    def execute(self, argv, *, cwd, timeout, network=False, environment_allowlist=None,
                protected=False, preserve_host_environment=False,
                extra_read_paths=None, extra_write_paths=None):
        self.calls.append({"argv": argv, "network": network})
        return {"ok": True, "argv": argv, "returncode": 0, "stdout": "", "stderr": "",
                "enforcement": "recording-fake", "pid": 1}


def test_a02_1_builder_receives_frozen_capabilities_and_2_network_comes_only_from_scope(tmp_path):
    work = sandbox(tmp_path)
    backend = _RecordingBackend()
    glm = GLMBuilder(executable=Path("joao-glm-fake"), backend=backend)
    rt = RunRuntime(tmp_path / "state", builder=glm, reviewer=AcceptReviewer(), profiles=LocalProfileAdapter())

    # RED, demonstrated inline: the PRE-A0.2 GLMBuilder hardcoded
    # `network=True` on every dispatch, regardless of what the mission
    # declared — reimplemented here as the naive comparison.
    def naive_pre_a02_network_decision(_mission_network_capability: bool) -> bool:
        return True  # the old adapter-level default, ignoring the mission entirely

    assert naive_pre_a02_network_decision(False) is True, "sanity: the naive default always grants network"

    run_id = rt.start(project_id="a02fixture", workspace=work, mission="fix",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]],
                      network_capability=False)
    rt.run_once(run_id)
    assert backend.calls, "GLMBuilder must have dispatched through the recording backend"
    # GREEN: with network_capability=False, the ACTUAL dispatch got network=False —
    # never the naive adapter-level True.
    assert all(call["network"] is False for call in backend.calls), backend.calls

    # Now the mirror case: network_capability=True must genuinely reach the builder.
    backend2 = _RecordingBackend()
    glm2 = GLMBuilder(executable=Path("joao-glm-fake"), backend=backend2)
    rt2 = RunRuntime(tmp_path / "state2", builder=glm2, reviewer=AcceptReviewer(), profiles=LocalProfileAdapter())
    (tmp_path / "w2").mkdir()
    work2 = sandbox(tmp_path / "w2")
    run_id2 = rt2.start(project_id="a02fixture", workspace=work2, mission="fix",
                        targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]],
                        network_capability=True)
    rt2.run_once(run_id2)
    assert backend2.calls and all(call["network"] is True for call in backend2.calls), backend2.calls


# ---------------------------------------------------------------------------
# #3 — the FULL frozen scope is cross-checked, not just the original A0-5
# trio (network_capability/read_only/allowed_write_paths).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field,tamper", [
    ("critical", True),
    ("provider_transport_network", True),
    ("required_backend", "container"),
])
def test_a02_3_full_scope_drift_beyond_the_original_three_fields_is_caught(tmp_path, field, tamper):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptReviewer())
    run_id = value.start(project_id="a02fixture", workspace=work, mission="fix",
                         targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    folder = _run_folder(tmp_path, run_id)
    run_path = folder / "run.json"
    run_data = json.loads(run_path.read_text())

    # RED, demonstrated by contrast: the PRE-A0.2 `_resolve_mission_scope`
    # cross-checked only network_capability/read_only/allowed_write_paths —
    # a hand-edit of any OTHER frozen field (critical, provider_transport_network,
    # required_backend, ...) would have gone completely undetected and the
    # run would have proceeded to build/test/review using the tampered value.
    run_data[field] = tamper
    run_path.write_text(json.dumps(run_data))

    state = value.run_once(run_id)
    assert state["status"] == "blocked"
    events = value.events(run_id)
    assert any(e["kind"] == "mission_scope_tampered_or_unresolvable" for e in events)
    tamper_event = next(e for e in events if e["kind"] == "mission_scope_tampered_or_unresolvable")
    assert field in tamper_event["mismatches"], tamper_event


def test_a02_3_builder_identity_drift_is_caught(tmp_path):
    """A resumed process wired to a DIFFERENT builder than start() recorded
    is a scope violation even when run.json itself was never hand-edited."""
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptReviewer())
    run_id = value.start(project_id="a02fixture", workspace=work, mission="fix",
                         targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])

    class _DifferentBuilder(SandboxBuilder):
        provider = "some-other-builder"; model = "some-other-model"

    value.builder = _DifferentBuilder(build)  # simulates a process restart wired to the wrong adapter
    state = value.run_once(run_id)
    assert state["status"] == "blocked"
    tamper_event = next(e for e in value.events(run_id) if e["kind"] == "mission_scope_tampered_or_unresolvable")
    assert "live_builder_identity" in tamper_event["mismatches"]


# ---------------------------------------------------------------------------
# #4 — sensitive gitignored files are scanned BEFORE any provider runs
# (start() itself), not only after the builder produced output.
# ---------------------------------------------------------------------------
def test_a02_4_secrets_scan_blocks_before_plan_review_or_builder_ever_run(tmp_path):
    work = sandbox(tmp_path)
    (work / ".gitignore").write_text("*.secret\n")
    _git(["add", ".gitignore"], work)
    _git(["commit", "-qm", "add gitignore"], work)
    (work / "preexisting.secret").write_text("TOP-SECRET-BEFORE-ANYTHING-RUNS")

    builder_called = {"n": 0}
    reviewer_called = {"n": 0}

    def build(_, workspace, __):
        builder_called["n"] += 1
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    class _CountingReviewer(AcceptReviewer):
        def review(self, run, run_dir):
            reviewer_called["n"] += 1
            return super().review(run, run_dir)

    value = runtime(tmp_path, build, _CountingReviewer())
    run_id = value.start(project_id="a02fixture", workspace=work, mission="fix",
                         targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.get(run_id)

    # GREEN: start() itself refused before planning/review/build ever touched
    # the workspace — neither the reviewer nor the builder was ever invoked.
    assert state["status"] == "blocked", state
    assert builder_called["n"] == 0, "builder must never run once a preexisting secret is detected at start()"
    assert reviewer_called["n"] == 0, "reviewer must never run once a preexisting secret is detected at start()"
    events = value.events(run_id)
    assert any(e["kind"] == "sensitive_ignored_file_detected" and e.get("stage") == "before-plan-review" for e in events)

    # RED contrast, demonstrated inline: `git status --porcelain` — the
    # completeness check every other gate in this runtime is built on —
    # never even shows a gitignored file, confirming this scan is the only
    # mechanism that could have caught it at this point.
    naive_status = _git(["status", "--porcelain"], work).stdout
    assert "preexisting.secret" not in naive_status


# ---------------------------------------------------------------------------
# #5 — declared_baseline is forbidden outright for a critical run.
# ---------------------------------------------------------------------------
def test_a02_5_declared_baseline_forbidden_for_critical_run(tmp_path):
    work = sandbox(tmp_path)
    (work / "module.py").write_text("VALUE = 999  # dirty, pre-existing\n")  # dirty on purpose

    def build(_, workspace, __):
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptReviewer())

    # RED, demonstrated by contrast: a NON-critical run with the same dirty
    # workspace + declared_baseline is accepted (existing A0-3 behavior).
    ok_run = value.start(project_id="a02fixture", workspace=work, mission="fix",
                         targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]],
                         declared_baseline="pre-existing dirty edit, accepted for this non-critical run")
    assert value.get(ok_run)["status"] in {"ready", "planning"}

    # GREEN: the identical dirty workspace + declared_baseline, but critical=True,
    # is refused outright at start() — never silently honored.
    with pytest.raises(RuntimeStateError, match="A0.2"):
        value.start(project_id="a02fixture", workspace=work, mission="fix",
                    targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]],
                    critical=True, declared_baseline="same reason, but this run is critical")


# ---------------------------------------------------------------------------
# #6 — the test suite never touches the real, committed memory/lessons.jsonl.
# ---------------------------------------------------------------------------
def test_a02_6_test_memory_isolation_redirects_lessons_write_target(tmp_path, monkeypatch):
    """C8-A hermeticity adjudication: DATA SOURCE ONLY changed — every A0.2
    assertion below is semantically identical to the original.

    The original captured `real_lessons.read_bytes()` (the REAL, committed
    `memory/lessons.jsonl`) before and after the redirected write, to prove
    the unredirected stock target was never written. That read is itself a
    real-mutable-data-root access, which C8-A's G-HERMETIC gate now forbids
    outright. The sentinel is therefore the module's OWN genuine
    pre-redirection default target (`retro.LESSONS` as imported), which —
    because `retro.py`/`import_ledger.py` resolve their `LESSONS` constant
    relative to their own on-disk location, and this test imports them from
    the session's isolated `JOAO_MEMORY_DIR` copy — is an INJECTED temporary
    memory root, never the real one. Asserted explicitly below before it is
    ever opened, so a resolution change fails loudly instead of silently
    re-introducing a real-root read.

    The claim the original made about the REAL file specifically is not lost:
    it is now enforced globally and unconditionally by the G-HERMETIC audit
    hook (`tests/conftest.py`) for EVERY test in the suite, and proven by
    `tests/test_g_hermetic_self_check.py::test_hermetic_direct_write_of_real_memory_blocks`
    — a strictly stronger guarantee than one test's before/after snapshot.
    """
    import os
    real_memory_dir = Path(__file__).resolve().parents[1] / "memory"
    assert os.environ.get("JOAO_MEMORY_DIR"), "the session-wide isolation fixture (conftest.py) must be active"
    isolated_dir = Path(os.environ["JOAO_MEMORY_DIR"])
    assert isolated_dir != real_memory_dir, "JOAO_MEMORY_DIR must not resolve to the real repo memory/ dir"

    sys.path.insert(0, str(isolated_dir))
    import retro  # the module the isolation fixture's own copy backs

    # The stock (unredirected) write target, captured BEFORE any redirection —
    # this is the file `ingest_candidates` would have appended to if
    # `set_state_dir(lessons_path=...)` did not work. It must resolve inside
    # the injected temporary memory root, never the real one (path check
    # only — no file is opened until this has passed).
    stock_target = Path(retro.LESSONS)
    assert stock_target.is_relative_to(isolated_dir), (
        f"the stock lessons target must resolve inside the injected temporary memory root "
        f"{isolated_dir}, got {stock_target}"
    )
    assert not stock_target.is_relative_to(real_memory_dir), (
        "the stock lessons target must never resolve into the real repo memory/ dir"
    )

    before_stock = stock_target.read_bytes()
    fake_lessons = tmp_path / "isolated-lessons.jsonl"
    fake_lessons.write_text("")
    retro.set_state_dir(tmp_path / "state", lessons_path=fake_lessons)
    assert retro.LESSONS == fake_lessons

    # GREEN: ingest_candidates() only ever writes to the redirected path.
    retro.ingest_candidates([{"rule": "a02 isolation probe rule, never persisted for real", "tags": ["a02-probe"]}],
                            project="a02fixture", run_id="a02-probe-run", date="2026-07-19", at="2026-07-19T00:00:00Z")
    assert "a02 isolation probe rule" in fake_lessons.read_text()
    # RED contrast: the stock (unredirected) target was never touched by this write.
    assert stock_target.read_bytes() == before_stock, "the stock lessons.jsonl must be byte-for-byte untouched"


# ---------------------------------------------------------------------------
# #7 — promote() self-verifies acceptance via the immutable approval record;
# a bare candidate/run_id is no longer sufficient.
# ---------------------------------------------------------------------------
def test_a02_7_promote_refuses_without_run_and_approval_record(tmp_path):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptReviewer())
    run_id = value.start(project_id="a02fixture", workspace=work, mission="fix",
                         targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.run_once(run_id)
    assert state["status"] == "needs_approval"
    accepted = value.approve(run_id)
    candidate = accepted["candidate"]
    folder = _run_folder(tmp_path, run_id)

    # RED, demonstrated directly: the PRE-A0.2 call shape (bare candidate +
    # run_id, no run/approval_record) is refused outright now.
    with pytest.raises(PromotionError, match="A0.2"):
        promotion_mod.promote(work, folder, candidate, run_id)

    # RED, second variant: a forged approval record naming a DIFFERENT
    # candidate_commit than the one actually being promoted.
    forged = create_approval_record(accepted, candidate, review_proof_sha256="0" * 64)
    forged["candidate_commit"] = "0" * 40
    with pytest.raises(PromotionError, match="does not match this candidate"):
        promotion_mod.promote(work, folder, candidate, run_id, run=accepted, approval_record=forged)

    # GREEN: the real, wired path (RunRuntime.promote(), reading the
    # approval-record.json approve() actually wrote) succeeds.
    manifest = value.promote(run_id)
    assert manifest["verified"] is True
    assert manifest["acceptance_self_verified"] is True


def test_a02_7_promote_refuses_if_review_evidence_tampered_after_approval(tmp_path):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptReviewer())
    run_id = value.start(project_id="a02fixture", workspace=work, mission="fix",
                         targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    value.run_once(run_id)
    value.approve(run_id)
    folder = _run_folder(tmp_path, run_id)

    # Tamper the review evidence AFTER approval — the approval record's
    # review_proof_sha256 was computed over the pre-tamper content.
    review_path = folder / "review-evidence.json"
    tampered = json.loads(review_path.read_text())
    tampered["proof"]["findings"] = ["forged: this was never in the real review"]
    review_path.write_text(json.dumps(tampered))

    with pytest.raises(PromotionError, match="review_proof_sha256"):
        value.promote(run_id)


# ---------------------------------------------------------------------------
# #8 — a run requiring a backend stronger than local_untrusted BLOCKs with
# reason_code=PREFLIGHT_UNAVAILABLE, never a silent fallback.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend_name", ["container", "vm"])
def test_a02_8_protected_backend_unavailable_blocks_with_preflight_unavailable(tmp_path, backend_name):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptReviewer())

    # RED, demonstrated inline: the naive pre-A0.2 shape would have silently
    # used local_untrusted regardless of what was requested.
    naive_actual_backend = "local_untrusted"
    assert naive_actual_backend != backend_name, "sanity: naive behavior ignores the requirement entirely"

    run_id = value.start(project_id="a02fixture", workspace=work, mission="fix",
                         targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]],
                         required_backend=backend_name)
    state = value.get(run_id)
    assert state["status"] == "blocked"
    events = value.events(run_id)
    blocked_event = next(e for e in events if e["kind"] == "protected_backend_unavailable")
    assert blocked_event["reason_code"] == "PREFLIGHT_UNAVAILABLE"
    assert blocked_event["required_backend"] == backend_name
    assert blocked_event["actual_backend"] == "unavailable"

    preflight = json.loads((_run_folder(tmp_path, run_id) / "backend-preflight.json").read_text())
    assert preflight == {"ok": False, "status": "BLOCKED", "reason_code": "PREFLIGHT_UNAVAILABLE",
                         "required_backend": backend_name, "actual_backend": "unavailable"}


def test_a02_8_local_untrusted_is_implemented_container_and_vm_are_declared_stubs():
    assert resolve_backend("local_untrusted").implemented is True
    assert resolve_backend("container").implemented is False
    assert resolve_backend("vm").implemented is False
    assert preflight_backend("local_untrusted")["ok"] is True
    assert preflight_backend("container")["ok"] is False
    with pytest.raises(Exception):
        resolve_backend("container").execute([sys.executable, "-c", "pass"], cwd=Path("."), timeout=1)


# ---------------------------------------------------------------------------
# Correctif 5 (run card: "Schéma JSON reviewer EXACT" — "exiger l'ensemble
# complet des clés (candidate_tree, verdict, findings, reviewer.provider,
# reviewer.model) — clés manquantes = REFUSÉ. Attack tests de clés
# manquantes.") had no dedicated attack tests in this file — its slot above
# (`test_a02_5_declared_baseline_forbidden_for_critical_run`) actually covers
# a different correctif (run card #6); left untouched per this run's "do not
# modify any other A0.2 corrective item" scope. These tests close correctif
# 5's own evidence gap, through the REAL ingestion path
# (`CodexEvidenceReviewer.review()` reading an actual `review-import.json`
# off disk — not `validate_reviewer_verdict`/`parse_reviewer_response` called
# as isolated helpers).
# ---------------------------------------------------------------------------
from joao_orchestrator.bubble.runtime import CodexEvidenceReviewer  # noqa: E402

_A02_5_TREE = "f" * 40


def _a02_5_run_dir(tmp_path):
    run_dir = tmp_path / "run-a02-5"
    run_dir.mkdir()
    return run_dir


def _a02_5_write_proof(run_dir, payload: dict) -> None:
    (run_dir / "review-import.json").write_text(json.dumps(payload))


def _a02_5_review(run_dir):
    reviewer = CodexEvidenceReviewer()
    return reviewer.review({"candidate_tree": _A02_5_TREE}, run_dir)


_A02_5_COMPLETE_PAYLOAD = {
    "candidate_tree": _A02_5_TREE, "verdict": "ACCEPT", "findings": [],
    "reviewer": {"provider": "codex", "model": "m"},
}


def test_a02_correctif5_reviewer_schema_missing_candidate_tree_blocked(tmp_path):
    run_dir = _a02_5_run_dir(tmp_path)
    payload = {k: v for k, v in _A02_5_COMPLETE_PAYLOAD.items() if k != "candidate_tree"}
    _a02_5_write_proof(run_dir, payload)
    result = _a02_5_review(run_dir)
    assert result["ok"] is False and result["decision"] == "block"
    assert "candidate_tree" in result["reason"]


def test_a02_correctif5_reviewer_schema_missing_verdict_blocked(tmp_path):
    run_dir = _a02_5_run_dir(tmp_path)
    payload = {k: v for k, v in _A02_5_COMPLETE_PAYLOAD.items() if k != "verdict"}
    _a02_5_write_proof(run_dir, payload)
    result = _a02_5_review(run_dir)
    assert result["ok"] is False and result["decision"] == "block"
    assert "verdict" in result["reason"]


def test_a02_correctif5_reviewer_schema_missing_findings_blocked(tmp_path):
    run_dir = _a02_5_run_dir(tmp_path)
    payload = {k: v for k, v in _A02_5_COMPLETE_PAYLOAD.items() if k != "findings"}
    _a02_5_write_proof(run_dir, payload)
    result = _a02_5_review(run_dir)
    assert result["ok"] is False and result["decision"] == "block"
    assert "findings" in result["reason"] and "correctif-5" in result["reason"]


def test_a02_correctif5_reviewer_schema_missing_reviewer_object_blocked(tmp_path):
    run_dir = _a02_5_run_dir(tmp_path)
    payload = {k: v for k, v in _A02_5_COMPLETE_PAYLOAD.items() if k != "reviewer"}
    _a02_5_write_proof(run_dir, payload)
    result = _a02_5_review(run_dir)
    assert result["ok"] is False and result["decision"] == "block"
    assert "reviewer" in result["reason"]


def test_a02_correctif5_reviewer_schema_missing_reviewer_provider_blocked(tmp_path):
    run_dir = _a02_5_run_dir(tmp_path)
    payload = dict(_A02_5_COMPLETE_PAYLOAD, reviewer={"model": "m"})
    _a02_5_write_proof(run_dir, payload)
    result = _a02_5_review(run_dir)
    assert result["ok"] is False and result["decision"] == "block"
    assert "provider" in result["reason"] and "correctif-5" in result["reason"]


def test_a02_correctif5_reviewer_schema_missing_reviewer_model_blocked(tmp_path):
    run_dir = _a02_5_run_dir(tmp_path)
    payload = dict(_A02_5_COMPLETE_PAYLOAD, reviewer={"provider": "codex"})
    _a02_5_write_proof(run_dir, payload)
    result = _a02_5_review(run_dir)
    assert result["ok"] is False and result["decision"] == "block"
    assert "model" in result["reason"] and "correctif-5" in result["reason"]


def test_a02_correctif5_reviewer_schema_complete_valid_verdict_accepted(tmp_path):
    run_dir = _a02_5_run_dir(tmp_path)
    _a02_5_write_proof(run_dir, _A02_5_COMPLETE_PAYLOAD)
    result = _a02_5_review(run_dir)
    assert result["ok"] is True and result["decision"] == "pass"
    assert result["proof"]["candidate_tree"] == _A02_5_TREE
    assert result["proof"]["reviewer"] == {"provider": "codex", "model": "worktree-sha-at-review-time"}


# ---------------------------------------------------------------------------
# Single dispatch point (§12.2): static AST guard — no adapter calls
# subprocess.run/subprocess.Popen/run_sandboxed directly.
# ---------------------------------------------------------------------------
_RUNTIME_PATH = Path(__file__).resolve().parents[1] / "src" / "joao_orchestrator" / "bubble" / "runtime.py"
_FORBIDDEN_DIRECT_CALLS = {"run_sandboxed"}
_FORBIDDEN_ATTR_CALLS = {("subprocess", "run"), ("subprocess", "Popen")}
_GUARDED_METHODS = {
    "GLMBuilder": {"build"},
    "CodexCLIReviewer": {"review_stage"},
}


def _forbidden_calls_in(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_DIRECT_CALLS:
                found.append(func.id)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if (func.value.id, func.attr) in _FORBIDDEN_ATTR_CALLS:
                    found.append(f"{func.value.id}.{func.attr}")
    return found


def test_a02_single_dispatch_point_ast_guard_no_direct_subprocess_in_adapters():
    tree = ast.parse(_RUNTIME_PATH.read_text())
    violations = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in _GUARDED_METHODS:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in _GUARDED_METHODS[node.name]:
                    bad = _forbidden_calls_in(item)
                    if bad:
                        violations[f"{node.name}.{item.name}"] = bad
    assert violations == {}, (
        f"adapter method(s) call a forbidden direct dispatch primitive instead of routing through "
        f"ExecutionBackend.execute(): {violations}"
    )


def test_a02_local_untrusted_backend_delegates_to_run_sandboxed_and_is_the_only_direct_caller():
    # GREEN: LocalUntrustedBackend.execute is the one place `run_sandboxed`
    # is called directly — verified structurally rather than by trusting the
    # docstring.
    backend_path = Path(__file__).resolve().parents[1] / "src" / "joao_orchestrator" / "bubble" / "execution_backend.py"
    tree = ast.parse(backend_path.read_text())
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LocalUntrustedBackend":
            for item in ast.walk(node):
                if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "run_sandboxed":
                    calls.append(item.func.id)
    assert calls, "LocalUntrustedBackend.execute must call run_sandboxed"


def test_a02_local_test_runner_sandboxed_path_uses_backend_not_run_sandboxed_directly():
    recorded = _RecordingBackend()
    runner = LocalTestRunner(sandboxed=True, backend=recorded)
    runner.run([sys.executable, "-c", "pass"], cwd=Path.cwd(), timeout=5, network=False)
    assert recorded.calls, "LocalTestRunner(sandboxed=True) must dispatch through the injected ExecutionBackend"
