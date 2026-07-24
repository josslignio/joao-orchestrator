from __future__ import annotations

import json
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from joao_orchestrator.run_night.activation import (
    ActivationError,
    RunNightActivation,
    consume_activation,
    load_hmac_key,
    sha256_file,
    verify_tranche3_evidence,
)
from joao_orchestrator.run_night.artifacts import ArtifactError, parse_artifact_exact
from joao_orchestrator.run_night.controller import RunNightController
from joao_orchestrator.run_night.fingerprint import fingerprint_tree
from joao_orchestrator.run_night.models import (
    NightTask,
    NightTaskExecution,
    RunNightLimits,
    RunNightSpec,
)


def _repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = repo / "src/joao_orchestrator/bubble/write_tier_policy.py"
    policy.parent.mkdir(parents=True)
    policy.write_text("WRITE_TIER=False\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "core"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True).strip()
    return repo, policy, sha, tree


def _evidence(tmp_path: Path, tranche_sha: str, tranche_tree: str, core_sha: str, sec_sha: str):
    t3 = tmp_path / "t3.zip"
    with zipfile.ZipFile(t3, "w") as archive:
        archive.writestr("FINAL_SHA.txt", tranche_sha + "\n")
        archive.writestr("FINAL_TREE.txt", tranche_tree + "\n")
        archive.writestr("FAILED_GATES.txt", "")
        archive.writestr("WORKTREE_CLEAN.txt", "")
        archive.writestr("WORKTREE_CLEAN.exit_code", "0\n")
        archive.writestr("SEC_BOOT_BASE_SHA256.txt", sec_sha + "\n")
        archive.writestr("SEC_BOOT_FINAL_SHA256.txt", sec_sha + "\n")
        archive.writestr(
            "M9_CODEX_EXACT_REVIEW.json",
            json.dumps(
                {
                    "pass": True,
                    "status": "ACCEPT",
                    "candidate_sha": tranche_sha,
                    "accepted": 1,
                    "blocked": 0,
                }
            ),
        )
        archive.writestr(
            "M10_LIVE_VERDICT.json",
            json.dumps(
                {
                    "pass": True,
                    "exact_marker": True,
                    "worktree_unchanged": True,
                    "selected_provider": "codex-review",
                }
            ),
        )
        archive.writestr(
            "GLOBAL_COMPARISON.json",
            json.dumps(
                {
                    "pass": True,
                    "new_failures_introduced": 0,
                    "new_failed_nodes": [],
                    "removed_test_nodes": [],
                    "changed_failure_signatures": [],
                }
            ),
        )
        archive.writestr(
            "RAW_PROVIDER_TEXT_SCAN.json",
            json.dumps(
                {
                    "pass": True,
                    "raw_provider_text_files": 0,
                    "violations": [],
                }
            ),
        )

    t3c = tmp_path / "t3_closure.json"
    t3c.write_text(
        json.dumps(
            {
                "verdict": "TRANCHE3_GPT_PASS",
                "final_sha": tranche_sha,
                "final_tree": tranche_tree,
                "evidence_sha256": sha256_file(t3),
                "zero_open_p0": True,
                "zero_open_p1": True,
                "sec_boot": "INTACT",
                "sec_boot_sha256": sec_sha,
                "write_tier": "OFF",
            }
        ),
        encoding="utf-8",
    )

    rn = tmp_path / "rn.zip"
    with zipfile.ZipFile(rn, "w") as archive:
        archive.writestr("FAILED_GATES.txt", "")
        archive.writestr("FINAL_SHA.txt", core_sha + "\n")
        archive.writestr("WORKTREE_STATUS.txt", "")
        archive.writestr(
            "RUNNIGHT_MASTER_VERDICT.json",
            json.dumps(
                {
                    "schema_version": 3,
                    "verdict": "AWAITING_GPT_REVIEW",
                    "tranche3_base_sha": tranche_sha,
                    "final_sha": core_sha,
                    "write_tier": "OFF",
                    "candidate_build_enabled": False,
                }
            ),
        )

    rnc = tmp_path / "rn_closure.json"
    rnc.write_text(
        json.dumps(
            {
                "verdict": "RUNNIGHT_MASTER_GPT_PASS",
                "final_sha": core_sha,
                "evidence_sha256": sha256_file(rn),
                "zero_open_p0": True,
                "zero_open_p1": True,
                "candidate_build_enabled": False,
                "write_tier": "OFF",
            }
        ),
        encoding="utf-8",
    )
    return t3, t3c, rn, rnc


def _artifact(task_id: str, kind: str, sha: str, summary: str = "safe summary") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "artifact_id": "artifact-1",
            "task_id": task_id,
            "kind": kind,
            "title": "Architecture",
            "summary": summary,
            "decisions": ["Use typed contracts"],
            "steps": ["Implement gate"],
            "tests": ["Test fail-closed"],
            "risks": ["Provider unavailable"],
            "dependencies": ["SupervisorCore"],
            "open_questions": [],
            "source_sha": sha,
            "reviewer_verdict": "ACCEPT",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _spec(tmp_path: Path, repo: Path, sha: str, paths):
    root = tmp_path / "execution"
    root.mkdir()
    task = NightTask(
        "task-1",
        "joao",
        "Produce strict JSON.",
        str(root),
        "architecture_packet",
    )
    return RunNightSpec(
        "rn0",
        sha,
        "read_only",
        str(tmp_path / "state"),
        str(repo),
        *(str(path) for path in paths),
        tasks=(task,),
        limits=RunNightLimits(
            max_tasks=2,
            max_provider_calls=4,
            max_codex_calls=2,
        ),
    )


def _activation(spec: RunNightSpec, policy: Path, tranche_sha: str, key: bytes):
    now = datetime.now(timezone.utc)
    value = RunNightActivation(
        activation_id="rna-test",
        run_id=spec.run_id,
        spec_sha256=spec.spec_sha256,
        tranche3_sha=tranche_sha,
        runnight_core_sha=spec.authorized_sha,
        sec_boot_sha256=sha256_file(policy),
        tranche3_evidence_sha256=sha256_file(Path(spec.tranche3_evidence_path)),
        tranche3_closure_sha256=sha256_file(Path(spec.tranche3_closure_path)),
        runnight_evidence_sha256=sha256_file(Path(spec.runnight_evidence_path)),
        runnight_closure_sha256=sha256_file(Path(spec.runnight_closure_path)),
        m7_closed=True,
        m8_closed=True,
        m9_closed=True,
        m10_closed=True,
        tranche3_gpt_pass=True,
        runnight_core_gpt_pass=True,
        write_tier_off=True,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
        nonce="n" * 64,
        key_id="test",
    )
    return value.sign(key)


class Runner:
    def __init__(
        self,
        output,
        *,
        mutate=False,
        calls=2,
        families=("zai", "openai"),
        verdict="ACCEPT",
    ):
        self.output = output
        self.mutate = mutate
        self.calls = calls
        self.families = families
        self.verdict = verdict

    def execute(self, task, *, max_provider_calls):
        if self.mutate:
            (Path(task.execution_root) / "bad.txt").write_text("x", encoding="utf-8")
        return NightTaskExecution(
            ok=True,
            output=self.output,
            verdict=self.verdict,
            provider_calls=self.calls,
            codex_calls=1,
            context_bytes=100,
            provider_families=self.families,
            selected_provider="glm-chat",
        )


def _setup(tmp_path: Path):
    repo, policy, sha, tree = _repo(tmp_path)
    tranche = "a" * 40
    paths = _evidence(tmp_path, tranche, tree, sha, sha256_file(policy))
    spec = _spec(tmp_path, repo, sha, paths)
    key = b"k" * 32
    activation = _activation(spec, policy, tranche, key)
    return repo, policy, sha, tree, tranche, paths, spec, key, activation


def test_activation_hmac_and_actual_files(tmp_path):
    _, _, sha, _, _, _, spec, key, activation = _setup(tmp_path)
    result = RunNightController(
        spec,
        activation,
        Runner(_artifact("task-1", "architecture_packet", sha)),
        hmac_key=key,
    ).preflight()
    assert result["authorized_sha"] == sha
    bad = RunNightActivation(**{**activation.__dict__, "m9_closed": False})
    with pytest.raises(ActivationError, match="HMAC"):
        RunNightController(spec, bad, Runner(""), hmac_key=key).preflight()


def test_actual_tranche3_evidence_contract_is_verified(tmp_path):
    _, policy, _, tree, tranche, paths, _, _, _ = _setup(tmp_path)
    result = verify_tranche3_evidence(
        paths[0],
        paths[1],
        expected_sha=tranche,
        expected_sec_boot_sha256=sha256_file(policy),
    )
    assert result["final_sha"] == tranche
    assert result["final_tree"] == tree
    assert result["m9"] == "ACCEPT"
    assert result["m10_exact_marker"] is True


def test_tranche3_evidence_rejects_raw_provider_violation(tmp_path):
    _, policy, _, tree, tranche, paths, _, _, _ = _setup(tmp_path)
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(paths[0]) as source, zipfile.ZipFile(bad_zip, "w") as target:
        for name in source.namelist():
            if name == "RAW_PROVIDER_TEXT_SCAN.json":
                target.writestr(
                    name,
                    json.dumps(
                        {
                            "pass": False,
                            "raw_provider_text_files": 1,
                            "violations": ["reason.txt"],
                        }
                    ),
                )
            else:
                target.writestr(name, source.read(name))
    closure = json.loads(Path(paths[1]).read_text(encoding="utf-8"))
    closure["evidence_sha256"] = sha256_file(bad_zip)
    bad_closure = tmp_path / "bad_closure.json"
    bad_closure.write_text(json.dumps(closure), encoding="utf-8")
    with pytest.raises(ActivationError, match="raw provider"):
        verify_tranche3_evidence(
            bad_zip,
            bad_closure,
            expected_sha=tranche,
            expected_sec_boot_sha256=sha256_file(policy),
        )


def test_activation_rejects_changed_evidence_file(tmp_path):
    _, _, _, _, _, _, spec, key, activation = _setup(tmp_path)
    Path(spec.tranche3_closure_path).write_text("{}", encoding="utf-8")
    with pytest.raises(ActivationError):
        RunNightController(spec, activation, Runner(""), hmac_key=key).preflight()


def test_activation_is_one_time_and_atomic(tmp_path):
    _, _, sha, _, _, _, spec, key, activation = _setup(tmp_path)
    runner = Runner(_artifact("task-1", "architecture_packet", sha))
    report = RunNightController(spec, activation, runner, hmac_key=key).run()
    assert report.state == "completed"
    with pytest.raises(ActivationError, match="consumed"):
        RunNightController(spec, activation, runner, hmac_key=key).preflight()
    with pytest.raises(ActivationError, match="consumed"):
        consume_activation(activation, Path(spec.state_root))


def test_validated_artifact_is_persisted_but_raw_wrapper_is_not(tmp_path):
    _, _, sha, _, _, _, spec, key, activation = _setup(tmp_path)
    raw = _artifact("task-1", "architecture_packet", sha)
    report = RunNightController(spec, activation, Runner(raw), hmac_key=key).run()
    artifact_path = Path(spec.state_root) / "run_night/runs/rn0/artifacts/task-1.json"
    assert artifact_path.is_file()
    assert json.loads(artifact_path.read_text(encoding="utf-8"))["summary"] == "safe summary"
    combined = "\n".join(
        path.read_text(errors="ignore")
        for path in (Path(spec.state_root) / "run_night/runs/rn0").rglob("*")
        if path.is_file()
    )
    assert raw not in combined
    assert report.tasks_awaiting_approval == 1


def test_artifact_rejects_surrounding_text_and_secrets(tmp_path):
    with pytest.raises(ArtifactError):
        parse_artifact_exact(
            "prefix {}",
            expected_task_id="t",
            expected_kind="test_plan",
            expected_sha="a" * 40,
        )
    raw = _artifact("t", "test_plan", "a" * 40, summary="api_key=SUPERSECRET")
    with pytest.raises(ArtifactError, match="secret"):
        parse_artifact_exact(
            raw,
            expected_task_id="t",
            expected_kind="test_plan",
            expected_sha="a" * 40,
        )


def test_read_only_mutation_blocks(tmp_path):
    _, _, sha, _, _, _, spec, key, activation = _setup(tmp_path)
    report = RunNightController(
        spec,
        activation,
        Runner(_artifact("task-1", "architecture_packet", sha), mutate=True),
        hmac_key=key,
    ).run()
    assert report.state == "blocked"
    assert report.stop_reason == "READ_ONLY_MUTATION"


def test_git_control_plane_mutation_changes_fingerprint(tmp_path):
    repo, _, _, _, _, _, _, _, _ = _setup(tmp_path)
    before = fingerprint_tree(repo)
    subprocess.run(
        ["git", "config", "joao.auditMutation", "true"],
        cwd=repo,
        check=True,
    )
    after = fingerprint_tree(repo)
    assert before != after


def test_provider_budget_overrun_blocks_immediately(tmp_path):
    _, _, sha, _, _, _, spec, key, activation = _setup(tmp_path)
    report = RunNightController(
        spec,
        activation,
        Runner(_artifact("task-1", "architecture_packet", sha), calls=9),
        hmac_key=key,
    ).run()
    assert report.stop_reason == "PROVIDER_BUDGET_OVERRUN"


def test_independent_review_required(tmp_path):
    _, _, sha, _, _, _, spec, key, activation = _setup(tmp_path)
    report = RunNightController(
        spec,
        activation,
        Runner(
            _artifact("task-1", "architecture_packet", sha),
            families=("zai",),
        ),
        hmac_key=key,
    ).run()
    assert report.stop_reason == "INDEPENDENT_REVIEW_REQUIRED"


def test_key_file_permissions(tmp_path):
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o644)
    with pytest.raises(ActivationError, match="group/world"):
        load_hmac_key(key)
    key.chmod(0o600)
    assert load_hmac_key(key) == b"k" * 32
