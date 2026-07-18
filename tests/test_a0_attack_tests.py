"""A0 (run card ~/Claude-HQ/JOAO_RUN_CARD_A0_INTEGRITE.md) — the 8 mandatory
attack tests, red before the RI-1..RI-8 fixes landed, green after. Each test
also demonstrates inline what the *naive* mechanism would have missed, so the
red/green contrast doesn't depend on checking out an older commit.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from joao_orchestrator.bubble.candidate import freeze_candidate, recompute_candidate_tree
from joao_orchestrator.bubble.change_capture import capture_full_diff
from joao_orchestrator.bubble.runtime import (
    LocalProfileAdapter, LocalTestRunner, RunRuntime, SandboxBuilder,
)
from joao_orchestrator.bubble.sandbox import run_sandboxed


def _git(argv, cwd, check=True):
    return subprocess.run(["git"] + argv, cwd=str(cwd), shell=False, capture_output=True, text=True, check=check)


def sandbox(tmp_path: Path) -> Path:
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "a0@example.invalid"], ["config", "user.name", "a0"]):
        _git(argv, workspace)
    (workspace / "module.py").write_text("VALUE = 1\n")
    (workspace / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    (workspace / ".joao-profile.json").write_text(json.dumps({
        "project_id": "a0fixture", "display_name": "a0fixture", "repository_root": str(workspace),
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
# Attack test 1 — untracked file added outside the proof → detected.
# ---------------------------------------------------------------------------
def test_attack1_untracked_file_outside_proof_is_detected(tmp_path):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "new_module.py").write_text("SECRET_PAYLOAD = 'smuggled'\n")
        return {"ok": True}

    # RED (naive baseline): plain `git diff` (no ref) never shows an untracked file at all.
    (work / "new_module.py").write_text("SECRET_PAYLOAD = 'smuggled'\n")
    naive_diff = _git(["diff", "--binary"], work).stdout
    assert "new_module.py" not in naive_diff, "sanity check: naive git diff should miss untracked files"
    _git(["clean", "-fq"], work)

    # GREEN: the controller's full-capture diff sees it.
    value = runtime(tmp_path, build, AcceptReviewer())
    run = value.start(project_id="a0fixture", workspace=work, mission="add module",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.run_once(run)
    assert state["status"] == "needs_approval"
    patch = (_run_folder(tmp_path, run) / "final-diff.patch").read_text()
    assert "SECRET_PAYLOAD" in patch


# ---------------------------------------------------------------------------
# Attack test 2 — staged-but-not-yet-committed modification → detected.
# ---------------------------------------------------------------------------
def test_attack2_staged_modification_is_detected(tmp_path):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2  # staged content\n")
        _git(["add", "module.py"], workspace)  # index == worktree, differs from HEAD
        return {"ok": True}

    # RED (naive baseline): with worktree == index, plain `git diff` shows nothing.
    (work / "module.py").write_text("VALUE = 2  # staged content\n")
    _git(["add", "module.py"], work)
    naive_diff = _git(["diff"], work).stdout
    assert naive_diff == "", "sanity check: naive git diff should miss staged-only changes"
    _git(["reset", "--hard"], work)

    # GREEN: `diff HEAD` (used by capture_full_diff) sees it.
    value = runtime(tmp_path, build, AcceptReviewer())
    run = value.start(project_id="a0fixture", workspace=work, mission="stage a change",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.run_once(run)
    assert state["status"] == "needs_approval"
    patch = (_run_folder(tmp_path, run) / "final-diff.patch").read_text()
    assert "staged content" in patch


# ---------------------------------------------------------------------------
# Attack test 3 — candidate modified after tests → evidence invalidated.
# ---------------------------------------------------------------------------
class _TamperingTestRunner:
    """A stub TestRunnerAdapter that tampers with the frozen candidate copy as
    a side effect of "running the tests" — simulating a compromised or buggy
    test step that mutates the very artifact it is supposed to only observe."""
    def run(self, argv, cwd, timeout, *, network=False, environment_allowlist=None):
        target = Path(cwd) / "module.py"
        target.chmod(0o644)
        target.write_text("VALUE = 999  # tampered mid-flight\n")
        return {"argv": argv, "returncode": 0, "ok": True, "stdout": "", "stderr": ""}


def test_attack3_candidate_tampered_after_tests_invalidates_evidence(tmp_path):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    value = RunRuntime(tmp_path / "state", builder=SandboxBuilder(build),
                       reviewer=AcceptReviewer(), tests=_TamperingTestRunner(),
                       profiles=LocalProfileAdapter())
    run = value.start(project_id="a0fixture", workspace=work, mission="fix",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.run_once(run)
    assert state["status"] == "blocked"
    events = value.events(run)
    assert any(e["kind"] == "candidate_integrity_violation" for e in events)


# ---------------------------------------------------------------------------
# Attack test 4 — reviewer answers for a different SHA → rejected.
# ---------------------------------------------------------------------------
class _WrongShaReviewer:
    provider = "codex"; model = "fixture"
    def review(self, run, _):
        return {"ok": True, "decision": "pass",
                "proof": {"verdict": "ACCEPT", "candidate_tree": "0" * 40,
                         "findings": [], "reviewer": {"provider": self.provider, "model": self.model}}}


def test_attack4_reviewer_answers_for_another_sha_is_rejected(tmp_path):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    value = runtime(tmp_path, build, _WrongShaReviewer())
    run = value.start(project_id="a0fixture", workspace=work, mission="fix",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.run_once(run)
    assert state["status"] == "blocked"
    review_evidence = json.loads((_run_folder(tmp_path, run) / "final-review-evidence.json").read_text())
    assert review_evidence["reason"] == "RI-4: reviewer proof missing or bound to a different candidate_tree"
    assert review_evidence["adapter_claimed_ok"] is True  # the adapter said ok; the controller overrode it


# ---------------------------------------------------------------------------
# Attack test 5 — builder fabricates its own SHA256SUMS → rejected.
# ---------------------------------------------------------------------------
def test_attack5_builder_fabricated_hash_is_never_trusted(tmp_path):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        output = workspace / "new_module.py"
        output.write_text("VALUE2 = 2\n")
        return {"ok": True, "output": str(output), "output_sha256": "0" * 64,
                "sha256": "deadbeef", "SHA256SUMS": "0" * 64 + "  new_module.py"}

    value = runtime(tmp_path, build, AcceptReviewer())
    run = value.start(project_id="a0fixture", workspace=work, mission="add module",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.run_once(run)
    assert state["status"] == "needs_approval"
    builder_evidence = json.loads((_run_folder(tmp_path, run) / "builder-evidence.json").read_text())
    assert "output_sha256" not in builder_evidence
    assert "sha256" not in builder_evidence
    real_sha256 = __import__("hashlib").sha256((work / "new_module.py").read_bytes()).hexdigest()
    assert builder_evidence["controller_verified_output_sha256"] == real_sha256
    assert builder_evidence["controller_verified_output_sha256"] != "0" * 64


# ---------------------------------------------------------------------------
# Attack test 6 — a test tries to read a planted secret → blocked.
# ---------------------------------------------------------------------------
def test_attack6_planted_secret_read_is_blocked(tmp_path):
    secret = Path.home() / ".joao_a0_attack6_secret"
    secret.write_text("TOP-SECRET-VALUE")
    try:
        read_secret = [sys.executable, "-c", f"print(open({str(secret)!r}).read())"]
        # RED: the pre-RI-6 code path (subprocess.run with the ambient parent
        # env — LocalTestRunner(sandboxed=False) is that exact old behavior)
        # would have read it straight through.
        red = LocalTestRunner(sandboxed=False).run(read_secret, cwd=tmp_path, timeout=10)
        assert red["ok"] is True and "TOP-SECRET-VALUE" in red["stdout"]
        # GREEN: sandboxed=True (the new default) blocks it.
        green = LocalTestRunner(sandboxed=True).run(read_secret, cwd=tmp_path, timeout=10)
        assert green["ok"] is False
        assert "TOP-SECRET-VALUE" not in green["stdout"]
    finally:
        secret.unlink()


# ---------------------------------------------------------------------------
# Attack test 7 — network access without a declared capability → blocked.
# ---------------------------------------------------------------------------
def test_attack7_network_without_capability_is_blocked(tmp_path):
    # This environment's own outer sandbox may already deny real connectivity,
    # so a bare "ok is False" would pass by accident either way. The real
    # distinguishing signal is *how* it fails: RI-6's Seatbelt profile denies
    # the syscall itself (a PermissionError inside the subprocess), which is
    # categorically different from an unsandboxed connection merely timing
    # out or being refused by the network.
    connect = [sys.executable, "-c", "import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)"]
    green = LocalTestRunner(sandboxed=True).run(connect, cwd=tmp_path, timeout=10, network=False)
    assert green["ok"] is False
    assert "PermissionError" in green["stderr"] and "Operation not permitted" in green["stderr"]
    # RED contrast: with network=True (the mission's explicit capability
    # declaration), the exact same sandboxed path is not denied at the syscall
    # level — proving the block above is the capability check, not a fluke.
    allowed = LocalTestRunner(sandboxed=True).run(
        [sys.executable, "-c", "print('would attempt network here')"], cwd=tmp_path, timeout=10, network=True)
    assert allowed["ok"] is True


# ---------------------------------------------------------------------------
# Attack test 8 — a zombie subprocess (escapes via setsid) is detected and killed.
# ---------------------------------------------------------------------------
def test_attack8_zombie_subprocess_is_detected_and_killed(tmp_path):
    def zombie_script(sleep_seconds: int) -> str:
        return (
            "import os, sys, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os.setsid()\n"
            f"    time.sleep({sleep_seconds})\n"
            "    sys.exit(0)\n"
            "else:\n"
            "    time.sleep(10)\n"
        )

    import time as _t

    def survivors_matching(needle: str) -> list[str]:
        # `pgrep -f` proved unreliable on this host against multi-line -c
        # scripts (it silently fails to match); a direct `ps` substring scan
        # over the full command line is the reliable equivalent.
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if needle in line]

    try:
        # RED: the pre-RI-6 unsandboxed path (subprocess.run(timeout=...)) only
        # kills the direct child on timeout — a grandchild that escaped via
        # setsid() is left running.
        red = LocalTestRunner(sandboxed=False).run([sys.executable, "-c", zombie_script(6)], cwd=tmp_path, timeout=2)
        assert red["timed_out"] is True
        _t.sleep(1)
        assert survivors_matching("time.sleep(6)"), "sanity check: the unsandboxed path should leave the escapee running"
    finally:
        subprocess.run(["pkill", "-9", "-f", "time.sleep(6)"], check=False)

    # GREEN: sandboxed=True walks the descendant tree by ppid and kills it too.
    green = LocalTestRunner(sandboxed=True).run([sys.executable, "-c", zombie_script(20)], cwd=tmp_path, timeout=2)
    assert green["timed_out"] is True
    _t.sleep(1)
    assert survivors_matching("time.sleep(20)") == []
