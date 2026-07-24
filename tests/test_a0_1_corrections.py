"""A0.1 (run card ~/Claude-HQ orchestrator session, 2026-07-19) — correction
pass on `feat/joao-a0-integrite` in response to an external GPT counter-audit
(NO-GO, 6 findings) against the original A0/RI-1..RI-8 delivery. One (or, for
A0-2 and A0-5, several) dedicated red -> green attack test(s) per finding:

  A0-1  reviewer must run on the immutable candidate copy, never the workspace
  A0-2  strict JSON reviewer contract (no prose fallback, no forged metadata)
  A0-3  a declared baseline is genuinely frozen (real git object, not a label)
  A0-4  gitignored files in sensitive paths are inventoried and refused
  A0-5  RI-6/RI-7 completeness: fail-closed protected sandbox, OS limits,
        scope resolved from the frozen mission record
  A0-6  verified atomic promotion (checked return codes, immediate rollback)

Each test demonstrates inline what the PRE-A0.1 mechanism would have missed
or done wrong, mirroring the style of `tests/test_a0_attack_tests.py`.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from joao_orchestrator.bubble.candidate import freeze_baseline, freeze_candidate
from joao_orchestrator.bubble.reviewer_contract import parse_reviewer_response, validate_reviewer_verdict
from joao_orchestrator.bubble.runtime import (
    CodexCLIReviewer, LocalProfileAdapter, RunRuntime, RuntimeStateError, SandboxBuilder,
)
from joao_orchestrator.bubble.sandbox import run_sandboxed
import joao_orchestrator.bubble.promotion as promotion_mod
from joao_orchestrator.bubble.promotion import PromotionError


def _git(argv, cwd, check=True):
    return subprocess.run(["git"] + argv, cwd=str(cwd), shell=False, capture_output=True, text=True, check=check)


def sandbox(tmp_path: Path) -> Path:
    workspace = tmp_path / "fixture"
    workspace.mkdir()
    for argv in (["init", "-q"], ["config", "user.email", "a01@example.invalid"], ["config", "user.name", "a01"]):
        _git(argv, workspace)
    (workspace / "module.py").write_text("VALUE = 1\n")
    (workspace / ".joao-profile.json").write_text(json.dumps({
        "project_id": "a01fixture", "display_name": "a01fixture", "repository_root": str(workspace),
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
# A0-1 — the reviewer must run on the immutable candidate copy, never the
# mutable workspace.
# ---------------------------------------------------------------------------
def test_a01_reviewer_runs_on_candidate_copy_never_the_mutable_workspace(tmp_path, allow_test_write_tier):
    work = sandbox(tmp_path)
    marker = tmp_path / "reviewed-paths.txt"
    fake_codex_py = tmp_path / "fake_codex.py"
    fake_codex_py.write_text(
        "import sys, re, json\n"
        "argv = sys.argv[1:]\n"
        "c_index = argv.index('-C')\n"
        "reviewed_path = argv[c_index + 1]\n"
        "prompt = argv[-1]\n"
        f"with open({str(marker)!r}, 'a') as fh:\n"
        "    fh.write(reviewed_path + chr(10))\n"
        "m = re.search(r\"candidate_tree = '([0-9a-f]{40})'\", prompt)\n"
        "tree = m.group(1) if m else ''\n"
        "print(json.dumps({'candidate_tree': tree, 'verdict': 'ACCEPT', 'findings': [],\n"
        "                   'reviewer': {'provider': 'fake', 'model': 'fake'}}))\n"
    )
    wrapper = tmp_path / "fake-codex"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {str(fake_codex_py)} \"$@\"\n")
    wrapper.chmod(0o755)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    reviewer = CodexCLIReviewer(executable=str(wrapper))
    value = runtime(tmp_path, build, reviewer)
    run = value.start(project_id="a01fixture", workspace=work, mission="fix",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])

    # Between candidate freeze and the final review, something mutates the
    # LIVE workspace (not the frozen candidate) — the exact window a
    # pre-A0.1 reviewer bound to `run["workspace"]` would have been exposed
    # to. Nothing here tampers the candidate itself (that is ATTACK_TEST 3's
    # scenario); this only proves the workspace and the candidate diverge.
    class WorkspaceMutatingTestRunner:
        def run(self, argv, cwd, timeout, *, network=False, environment_allowlist=None, protected=False):
            (work / "module.py").write_text("VALUE = 999  # workspace drifted after candidate freeze\n")
            return {"argv": argv, "returncode": 0, "ok": True, "stdout": "", "stderr": ""}

    value.tests = WorkspaceMutatingTestRunner()
    state = value.run_once(run)
    assert state["status"] == "needs_approval"
    candidate = state["candidate"]

    reviewed_paths = marker.read_text().splitlines()
    # plan (no candidate yet, workspace is the CORRECT target), then build,
    # then final (both must be the candidate's read-only copy).
    assert len(reviewed_paths) == 3
    assert reviewed_paths[0] == str(work)
    assert reviewed_paths[1] == candidate["readonly_copy"]
    assert reviewed_paths[2] == candidate["readonly_copy"]

    # RED contrast, demonstrated inline: by now the live workspace and the
    # candidate genuinely differ — a reviewer pointed at `run["workspace"]`
    # (the pre-A0.1 mechanism) for the build/final stage would have reviewed
    # the WRONG, drifted content instead of what was actually tested.
    assert (work / "module.py").read_text() != (Path(candidate["readonly_copy"]) / "module.py").read_text()

    build_review = json.loads((_run_folder(tmp_path, run) / "build-review-evidence.json").read_text())
    final_review = json.loads((_run_folder(tmp_path, run) / "final-review-evidence.json").read_text())
    assert build_review["reviewed_path"] == candidate["readonly_copy"]
    assert build_review["candidate_commit"] == candidate["candidate_commit"]
    assert final_review["reviewed_path"] == candidate["readonly_copy"]
    assert final_review["recomputed_tree_before_review"] == candidate["candidate_tree"]
    assert final_review["recomputed_tree_after_review"] == candidate["candidate_tree"]


# ---------------------------------------------------------------------------
# A0-2a — prose before/after a valid JSON verdict object is refused (the old
# brace-scanning fallback would have accepted it).
# ---------------------------------------------------------------------------
def _naive_pre_a02_parse(raw_text):
    """Reimplements the exact pre-A0.1 brace-scanning fallback inline, only
    to demonstrate the RED baseline this correction removes — the shipped
    `parse_reviewer_response` no longer contains this path at all."""
    text = (raw_text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    depth = 0; start = None; candidates = []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start:i + 1])
    for chunk in reversed(candidates):
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def test_a02_prose_around_json_is_refused(tmp_path):
    tree = "a" * 40
    payload = {"candidate_tree": tree, "verdict": "ACCEPT", "findings": [],
               "reviewer": {"provider": "codex", "model": "m"}}
    raw = "Sure, here is my verdict:\n" + json.dumps(payload) + "\nHope that helps!"

    # RED: the old brace-scanning fallback finds and accepts the embedded object.
    naive = _naive_pre_a02_parse(raw)
    assert naive is not None and naive["verdict"] == "ACCEPT"

    # GREEN: the shipped strict parser refuses outright.
    assert parse_reviewer_response(raw) is None
    result = validate_reviewer_verdict(raw, expected_candidate_tree=tree, provider="codex-subscription",
                                       model="real-model", returncode=0)
    assert result["ok"] is False
    assert result["decision"] == "block"
    assert "strict" in result["reason"]


# ---------------------------------------------------------------------------
# A0-2b — a non-zero reviewer process return code is never masked by an
# otherwise well-formed ACCEPT payload in stdout.
# ---------------------------------------------------------------------------
def test_a02_nonzero_returncode_refused_despite_valid_accept_payload(tmp_path):
    tree = "b" * 40
    raw = json.dumps({"candidate_tree": tree, "verdict": "ACCEPT", "findings": [],
                      "reviewer": {"provider": "codex", "model": "m"}})

    # RED contrast: the pre-A0.1 signature had no `returncode` parameter at
    # all — a schema-valid ACCEPT payload was accepted regardless of how the
    # reviewer process actually exited.
    naive_ok = validate_reviewer_verdict(raw, expected_candidate_tree=tree, provider="codex", model="m")["ok"]
    assert naive_ok is True  # returncode defaults to 0 when the caller omits it — same content, no exit-code signal

    # GREEN: passing the real (non-zero) exit code refuses the verdict.
    result = validate_reviewer_verdict(raw, expected_candidate_tree=tree, provider="codex", model="m", returncode=1)
    assert result["ok"] is False
    assert result["decision"] == "block"


# ---------------------------------------------------------------------------
# A0-2c — forged reviewer.provider/reviewer.model in the response body is
# silently overwritten by the controller-computed identity, never trusted.
# ---------------------------------------------------------------------------
def test_a02_forged_reviewer_metadata_is_overwritten(tmp_path):
    tree = "c" * 40
    raw = json.dumps({"candidate_tree": tree, "verdict": "ACCEPT", "findings": [],
                      "reviewer": {"provider": "FORGED-PROVIDER", "model": "FORGED-MODEL"}})
    result = validate_reviewer_verdict(raw, expected_candidate_tree=tree,
                                       provider="codex-subscription", model="local-codex-review", returncode=0)
    assert result["ok"] is True
    # GREEN: the accepted evidence carries the CONTROLLER's identity, never
    # the forged one the response body actually claimed.
    assert result["proof"]["reviewer"] == {"provider": "codex-subscription", "model": "local-codex-review"}
    assert "FORGED-PROVIDER" not in json.dumps(result["proof"])


# ---------------------------------------------------------------------------
# A0-3 — a declared baseline is genuinely frozen (a real git object), and a
# pre-existing modification is distinguishable from the builder's own work.
# ---------------------------------------------------------------------------
def test_a03_baseline_genuinely_frozen_and_distinguishable_from_builder_work(tmp_path, allow_test_write_tier):
    work = sandbox(tmp_path)
    # A pre-existing, uncommitted modification in an allowed path, present
    # BEFORE the run ever starts.
    (work / "module.py").write_text("VALUE = 1  # pre-existing dirty edit\n")

    def build(_, workspace, __):
        current = (workspace / "module.py").read_text()
        (workspace / "module.py").write_text(current + "BUILDER_ADDED = True\n")
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptReviewer())
    run = value.start(project_id="a01fixture", workspace=work, mission="fix on top of a dirty baseline",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]],
                      declared_baseline="pre-existing pending edit, explicitly accepted for this run")
    run_record = value.get(run)
    baseline = run_record["baseline"]
    assert baseline is not None

    # GREEN: a real, resolvable git commit object was created — not just a
    # string label (the pre-A0.1 `baseline_frozen_and_declared` behavior).
    exists = _git(["cat-file", "-e", baseline["baseline_commit"] + "^{commit}"], work, check=False).returncode == 0
    assert exists
    events = value.events(run)
    assert any(e["kind"] == "baseline_frozen" for e in events)

    state = value.run_once(run)
    assert state["status"] == "needs_approval"
    folder = _run_folder(tmp_path, run)

    # RED, demonstrated inline: a single `git diff HEAD` (the pre-A0.1
    # picture — `final-diff.patch` is still computed this way, unchanged)
    # conflates the pre-existing dirty edit and the builder's own change
    # into one indistinguishable blob.
    total_diff = (folder / "final-diff.patch").read_text()
    assert "pre-existing dirty edit" in total_diff and "BUILDER_ADDED" in total_diff

    # GREEN: the builder-only diff (against the frozen baseline_commit)
    # isolates exactly what the builder itself changed. The pre-existing
    # line may still appear as unified-diff CONTEXT (unprefixed), but it
    # must never be marked as something the builder added or changed —
    # that "+"-prefixed distinction is exactly what makes it distinguishable
    # from the builder's own work rather than silently absorbed into it.
    builder_only = (folder / "builder-only-diff.patch").read_text()
    assert "+BUILDER_ADDED = True" in builder_only
    assert "+VALUE = 1  # pre-existing dirty edit" not in builder_only

    # GREEN: the baseline-drift patch (written at start(), before the
    # builder ever ran) isolates exactly what was already dirty — as an
    # actual change (the base commit had plain "VALUE = 1", so the dirty
    # edit shows as a genuine "+" line here), and never includes the
    # builder's own later addition.
    drift = (folder / "baseline-drift.patch").read_text()
    assert "+VALUE = 1  # pre-existing dirty edit" in drift
    assert "BUILDER_ADDED" not in drift


# ---------------------------------------------------------------------------
# A0-4 — gitignored files landing in a sensitive runtime path are detected,
# refused, and (defense in depth) do not survive promotion even if one
# somehow reached the workspace by another route.
# ---------------------------------------------------------------------------
def test_a04_gitignored_sensitive_file_detected_refused_and_does_not_survive_promotion(tmp_path, allow_test_write_tier):
    work = sandbox(tmp_path)
    (work / ".gitignore").write_text("*.secret\n")
    _git(["add", ".gitignore"], work)
    _git(["commit", "-qm", "add gitignore"], work)

    # --- detected + refused during the run itself ---
    def bad_build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        (workspace / "payload.secret").write_text("TOP-SECRET-PAYLOAD")
        return {"ok": True}

    value = runtime(tmp_path, bad_build, AcceptReviewer())
    bad_run = value.start(project_id="a01fixture", workspace=work, mission="fix",
                          targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    bad_state = value.run_once(bad_run)
    assert bad_state["status"] == "blocked"
    bad_folder = _run_folder(tmp_path, bad_run)
    ignored_evidence = json.loads((bad_folder / "ignored-files-evidence.json").read_text())
    assert "payload.secret" in ignored_evidence["ignored_files"]
    assert any(e["kind"] == "sensitive_ignored_file_detected" for e in value.events(bad_run))

    # RED contrast, demonstrated inline: `git status --porcelain` — what
    # every other completeness check in this runtime is built on — never
    # even shows a gitignored file.
    naive_status = _git(["status", "--porcelain"], work).stdout
    assert "payload.secret" not in naive_status
    (work / "payload.secret").unlink()
    _git(["reset", "--hard"], work)

    # --- does not survive promotion, even granting a stray ignored file
    # sitting in the workspace at promotion time (A0-6 sterile worktree) ---
    def good_build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    value2 = runtime(tmp_path, good_build, AcceptReviewer())
    run = value2.start(project_id="a01fixture", workspace=work, mission="fix",
                       targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value2.run_once(run)
    assert state["status"] == "needs_approval"
    accepted = value2.approve(run)
    candidate = accepted["candidate"]
    (work / "leftover.secret").write_text("SHOULD-NOT-SURVIVE-PROMOTION")
    folder = _run_folder(tmp_path, run)
    manifest = value2.promote(run)
    sterile = Path(manifest["promoted_worktree"])

    # GREEN: the sterile worktree is a fresh checkout of the candidate
    # commit's own tree only — the stray ignored file cannot appear in it.
    assert not (sterile / "leftover.secret").exists()
    assert manifest["verified"] is True

    # RED contrast, demonstrated inline: the pre-A0.1 mechanism (`git reset
    # --hard` on the LIVE workspace) never removes an untracked/ignored
    # file — confirmed directly against `work`, the very worktree that
    # mechanism would have promoted into.
    _git(["reset", "--hard", candidate["candidate_commit"]], work, check=False)
    assert (work / "leftover.secret").exists(), "sanity check: reset --hard never touches ignored files"


# ---------------------------------------------------------------------------
# A0-5a — a process-bomb is bounded by the per-dispatch RLIMIT_NPROC ceiling.
# ---------------------------------------------------------------------------
_FORKBOMB_SCRIPT = (
    "import os, time\n"
    "n = 0\n"
    "try:\n"
    "    while n < 300:\n"          # a hard safety valve independent of any OS limit
    "        pid = os.fork()\n"
    "        if pid == 0:\n"
    "            time.sleep(0.3)\n"
    "            os._exit(0)\n"
    "        n += 1\n"
    "except OSError:\n"
    "    pass\n"
    "print('forked', n)\n"
)


def test_a05_process_bomb_is_bounded_by_nproc_ceiling(tmp_path):
    # GREEN: a small headroom budget above the current per-UID process count
    # stops the fork loop almost immediately with an OSError from fork()
    # itself — nowhere near the script's own 300-iteration safety valve.
    limited = run_sandboxed([sys.executable, "-c", _FORKBOMB_SCRIPT], cwd=tmp_path, timeout=10,
                            max_new_processes=8, memory_bytes=None)
    assert limited["ok"] is True
    forked_limited = int(limited["stdout"].strip().split()[-1])
    assert forked_limited < 150

    # RED contrast, demonstrated inline: with the process-count limit
    # disabled, the SAME script reaches its own 300-fork safety valve
    # instead of ever being stopped by an OS-enforced ceiling.
    unlimited = run_sandboxed([sys.executable, "-c", _FORKBOMB_SCRIPT], cwd=tmp_path, timeout=10,
                              max_new_processes=None, memory_bytes=None)
    forked_unlimited = int(unlimited["stdout"].strip().split()[-1])
    assert forked_unlimited == 300
    assert forked_limited < forked_unlimited


# ---------------------------------------------------------------------------
# A0-5b — memory exhaustion is caught by the RSS watchdog. `RLIMIT_AS` is
# set as defense-in-depth but is NOT the mechanism relied on here: verified
# empirically to be unreliable on macOS/Darwin for mmap-backed allocations
# (a real `setrlimit(RLIMIT_AS, 200MB)` in this test's own dev environment
# let a 20 GB allocation through untouched) — see `sandbox.py`'s docstring.
# ---------------------------------------------------------------------------
_MEMHOG_SCRIPT = (
    "blocks = []\n"
    "for _ in range(4000):\n"
    "    blocks.append(bytearray(10 * 1024 * 1024))\n"
    "print('allocated', len(blocks) * 10, 'MB')\n"
)


def test_a05_memory_exhaustion_is_caught_by_rss_watchdog(tmp_path):
    limited = run_sandboxed([sys.executable, "-c", _MEMHOG_SCRIPT], cwd=tmp_path, timeout=15,
                            memory_bytes=200 * 1024 * 1024, max_new_processes=None)
    # GREEN: killed well before it could allocate anywhere near 40 GB.
    assert limited["ok"] is False
    assert limited["memory_limit_exceeded"] is True
    assert "allocated" not in limited["stdout"]

    # RED contrast, demonstrated inline: with no memory ceiling, the exact
    # same script completes and reports a large allocation, proving the
    # kill above was the watchdog, not a coincidental crash.
    unlimited = run_sandboxed([sys.executable, "-c", _MEMHOG_SCRIPT], cwd=tmp_path, timeout=15,
                              memory_bytes=None, max_new_processes=None)
    assert unlimited["ok"] is True
    assert "allocated 40000 MB" in unlimited["stdout"]


# ---------------------------------------------------------------------------
# A0-5c — a double-fork ("daemonize") reparenting escape is caught. The
# reparented grandchild's ppid becomes 1 once its immediate parent exits,
# which structurally defeats a ppid-only descendant walk; the per-dispatch
# environment token sweep catches it anyway.
# ---------------------------------------------------------------------------
def test_a05_double_fork_reparenting_escape_is_caught(tmp_path):
    script_path = tmp_path / "doublefork.py"
    script_path.write_text(
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    pid2 = os.fork()\n"
        "    if pid2 == 0:\n"
        "        time.sleep(20)\n"          # the escapee: reparented to PID 1
        "        sys.exit(0)\n"
        "    else:\n"
        "        sys.exit(0)\n"             # intermediate parent exits -> orphan reparented
        "else:\n"
        "    time.sleep(10)\n"              # keeps the tracked root PID alive long enough to time out
    )

    def real_survivors():
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if "doublefork.py" in line and len(line) < 200]

    result = run_sandboxed([sys.executable, str(script_path)], cwd=tmp_path, timeout=2,
                           max_new_processes=None, memory_bytes=None)
    assert result["timed_out"] is True
    time.sleep(1)
    # GREEN: the token sweep finds and kills the reparented grandchild even
    # though a ppid-only walk from the tracked root PID could never reach it.
    assert real_survivors() == []


# ---------------------------------------------------------------------------
# A0-5d — network_capability/read_only/allowed_write_paths are resolved from
# the run's own frozen mission-scope record; a live run.json edited after
# start() to widen scope is caught and refused, not silently trusted.
# ---------------------------------------------------------------------------
def test_a05_forged_tampered_signed_scope_is_caught(tmp_path):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptReviewer())
    run = value.start(project_id="a01fixture", workspace=work, mission="fix",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]],
                      network_capability=False)
    run_path = _run_folder(tmp_path, run) / "run.json"
    live = json.loads(run_path.read_text())
    assert live["network_capability"] is False

    # RED, demonstrated inline: the pre-A0.1 code read `network_capability`
    # straight off this same live `run` dict with no cross-check at all —
    # so hand-editing run.json directly (bypassing every public API) would
    # have silently smuggled a network capability into the dispatch.
    live["network_capability"] = True
    run_path.write_text(json.dumps(live, indent=2, sort_keys=True))

    # GREEN: run_once() cross-checks the live run record against the frozen
    # checkpoints/0000-pending.json written once at start() and refuses.
    state = value.run_once(run)
    assert state["status"] == "blocked"
    events = value.events(run)
    assert any(e["kind"] == "mission_scope_tampered_or_unresolvable" for e in events)
    tamper_event = next(e for e in events if e["kind"] == "mission_scope_tampered_or_unresolvable")
    assert "network_capability" in tamper_event["mismatches"]


# ---------------------------------------------------------------------------
# A0-6 — a worktree sync failure AFTER the branch ref compare-and-swap has
# already succeeded must never produce a false success; the ref is
# immediately CAS-rolled-back instead.
# ---------------------------------------------------------------------------
def test_a06_worktree_sync_failure_after_ref_cas_never_produces_false_success(tmp_path, monkeypatch, allow_test_write_tier):
    work = sandbox(tmp_path)

    def build(_, workspace, __):
        (workspace / "module.py").write_text("VALUE = 2\n")
        return {"ok": True}

    value = runtime(tmp_path, build, AcceptReviewer())
    run = value.start(project_id="a01fixture", workspace=work, mission="fix",
                      targeted_tests=[], full_tests=[[sys.executable, "-c", "pass"]])
    state = value.run_once(run)
    assert state["status"] == "needs_approval"
    accepted = value.approve(run)
    candidate = accepted["candidate"]
    folder = _run_folder(tmp_path, run)

    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], work).stdout.strip()
    tip_before = _git(["rev-parse", branch], work).stdout.strip()

    real_git = promotion_mod._git

    def flaky_git(argv, cwd, check=True):
        if argv[:2] == ["worktree", "add"]:
            return subprocess.CompletedProcess(argv, returncode=1, stdout="",
                                               stderr="simulated worktree sync failure")
        return real_git(argv, cwd, check=check)

    # RED, demonstrated by contrast: the pre-A0.1 code path called `git
    # reset --hard` with `check=False` and never inspected its return code
    # at all — a failure exactly like this one would have been silently
    # swallowed, leaving the branch ref already moved with no verification
    # that anything downstream actually reflected it.
    monkeypatch.setattr(promotion_mod, "_git", flaky_git)
    with pytest.raises(PromotionError, match="A0-6"):
        value.promote(run)

    # GREEN: the branch ref was CAS-rolled-back immediately — never left
    # pointing at an unmaterialized commit — and no manifest was written.
    tip_after = _git(["rev-parse", branch], work).stdout.strip()
    assert tip_after == tip_before
    assert not (folder / "promotion-manifest.json").exists()
