from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from joao_orchestrator.bubble import candidate as candidate_mod
from joao_orchestrator.bubble.candidate import CandidateError, freeze_candidate, verify_candidate_identity
from joao_orchestrator.bubble.promotion import (
    PromotionError, create_approval_record, promote, write_approval_record,
)
from joao_orchestrator.bubble.reviewer_contract import validate_reviewer_verdict
from joao_orchestrator.control_plane.checkpoint import CheckpointStore
from joao_orchestrator.integrity.records import IntegrityKeyManager
from joao_orchestrator.storage.persistence_errors import CheckpointCorruptionError


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "m5@example.invalid")
    _git(repo, "config", "user.name", "M5")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "base")
    return repo


def _index_snapshot(repo: Path) -> tuple[bool, bytes]:
    path = Path(_git(repo, "rev-parse", "--git-path", "index").stdout.strip())
    if not path.is_absolute():
        path = repo / path
    return path.exists(), path.read_bytes() if path.exists() else b""


def _signed_review(candidate: dict, key: bytes, *, returncode: int = 0):
    raw = json.dumps({
        "candidate_tree": candidate["candidate_tree"],
        "verdict": "ACCEPT",
        "findings": [],
        "reviewer": {"provider": "untrusted", "model": "untrusted"},
    })
    return validate_reviewer_verdict(
        raw,
        expected_candidate_tree=candidate["candidate_tree"],
        provider="codex-subscription",
        model="exact-sha-review",
        returncode=returncode,
        expected_identity=candidate["identity_v2"],
        identity_signature=candidate["identity_signature"],
        hmac_key=key,
    )


def _approval_fixture(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    key = IntegrityKeyManager(tmp_path / "state" / "integrity").get_key()
    candidate = freeze_candidate(repo, run_dir, "run-m5", 1, hmac_key=key)
    review = _signed_review(candidate, key)
    assert review["ok"] is True
    (run_dir / "review-evidence.json").write_text(json.dumps(review, sort_keys=True), encoding="utf-8")
    run = {
        "run_id": "run-m5",
        "status": "accepted",
        "review_verified": True,
        "candidate_tree": candidate["candidate_tree"],
        "final_review_proof": review["proof"],
    }
    review_sha = __import__("hashlib").sha256((run_dir / "review-evidence.json").read_bytes()).hexdigest()
    approval = create_approval_record(run, candidate, review_proof_sha256=review_sha, hmac_key=key)
    write_approval_record(run_dir, tmp_path / "approvals.jsonl", approval)
    return repo, run_dir, key, candidate, run, approval


def test_real_freeze_preserves_preexisting_staged_index(tmp_path):
    repo = _repo(tmp_path)
    (repo / "staged.txt").write_text("keep staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    before = _index_snapshot(repo)
    key = IntegrityKeyManager(tmp_path / "state" / "integrity").get_key()
    candidate = freeze_candidate(repo, tmp_path / "run", "run-index", 1, hmac_key=key)
    assert _index_snapshot(repo) == before
    assert verify_candidate_identity(candidate, repo, key).candidate_tree == candidate["candidate_tree"]


def test_real_freeze_failure_preserves_index(monkeypatch, tmp_path):
    repo = _repo(tmp_path)
    (repo / "staged.txt").write_text("keep staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")
    before = _index_snapshot(repo)
    original = candidate_mod._git

    def injected(argv, workspace, **kwargs):
        if argv[:2] == ["worktree", "add"]:
            raise CandidateError("injected freeze failure")
        return original(argv, workspace, **kwargs)

    monkeypatch.setattr(candidate_mod, "_git", injected)
    key = IntegrityKeyManager(tmp_path / "state" / "integrity").get_key()
    with pytest.raises(CandidateError):
        freeze_candidate(repo, tmp_path / "run", "run-fail", 1, hmac_key=key)
    assert _index_snapshot(repo) == before


def test_real_freeze_keeps_absent_index_absent(tmp_path):
    repo = _repo(tmp_path)
    index = Path(_git(repo, "rev-parse", "--git-path", "index").stdout.strip())
    if not index.is_absolute():
        index = repo / index
    index.unlink()
    assert not index.exists()
    # With no live index, create an unstaged worktree change; the temporary
    # index must still be initialized from HEAD and the live index left absent.
    (repo / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    key = IntegrityKeyManager(tmp_path / "state" / "integrity").get_key()
    freeze_candidate(repo, tmp_path / "run", "run-no-index", 1, hmac_key=key)
    assert not index.exists()


def test_reviewer_v2_nonzero_returncode_blocks_accept(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    key = IntegrityKeyManager(tmp_path / "state" / "integrity").get_key()
    candidate = freeze_candidate(repo, tmp_path / "run", "run-review", 1, hmac_key=key)
    result = _signed_review(candidate, key, returncode=9)
    assert result["ok"] is False
    assert result["decision"] == "block"
    assert result["proof"]["reviewer_record_v2"]["reviewer_return_code"] == 9


def test_real_promote_verifies_signed_chain_and_cas(tmp_path):
    repo, run_dir, key, candidate, run, approval = _approval_fixture(tmp_path)
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    manifest = promote(repo, run_dir, candidate, run["run_id"], branch=branch,
                       run=run, approval_record=approval, hmac_key=key)
    assert manifest["verified"] is True
    assert _git(repo, "rev-parse", branch).stdout.strip() == candidate["candidate_commit"]


def test_real_promote_rejects_tampered_approval(tmp_path):
    repo, run_dir, key, candidate, run, approval = _approval_fixture(tmp_path)
    approval["approval_record_v2"]["approved_by"] = "attacker"
    with pytest.raises(PromotionError, match="approval signature"):
        promote(repo, run_dir, candidate, run["run_id"], run=run,
                approval_record=approval, hmac_key=key)


def test_real_promote_rejects_post_review_candidate_mutation(tmp_path):
    repo, run_dir, key, candidate, run, approval = _approval_fixture(tmp_path)
    target = Path(candidate["readonly_copy"]) / "app.py"
    target.chmod(0o644)
    target.write_text("VALUE = 999\n", encoding="utf-8")
    with pytest.raises(PromotionError, match="candidate identity revalidation"):
        promote(repo, run_dir, candidate, run["run_id"], run=run,
                approval_record=approval, hmac_key=key)


def test_checkpoint_store_rejects_tamper_and_wrong_key(tmp_path):
    store = CheckpointStore(tmp_path / "state", "cp")
    store.save_state({"value": 1})
    raw = json.loads(store.state_path.read_text(encoding="utf-8"))
    raw["value"] = 2
    store.state_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointCorruptionError):
        store.load_state()

    other = CheckpointStore(tmp_path / "other", "cp")
    other.state_path.parent.mkdir(parents=True, exist_ok=True)
    other.state_path.write_bytes(store.state_path.read_bytes())
    with pytest.raises(CheckpointCorruptionError):
        other.load_state()


def test_integrity_permissions_and_no_key_leak(tmp_path):
    manager = IntegrityKeyManager(tmp_path / "integrity")
    key = manager.get_key()
    assert len(key) == 32
    assert (manager.state_dir.stat().st_mode & 0o777) == 0o700
    assert (manager.key_path.stat().st_mode & 0o777) == 0o600
    assert key.hex() not in repr(manager.__dict__)

def test_runtime_real_freeze_review_approval_chain(monkeypatch, tmp_path):
    from joao_orchestrator.bubble import write_tier_policy
    from joao_orchestrator.bubble.runtime import LocalProfileAdapter, RunRuntime, SandboxBuilder

    monkeypatch.setattr(write_tier_policy, "assert_write_tier_enabled", lambda *a, **k: None)
    repo = _repo(tmp_path)
    (repo / ".joao-profile.json").write_text(json.dumps({
        "project_id": "m5runtime", "display_name": "m5runtime",
        "repository_root": str(repo), "allowed_write_paths": ["app.py"],
        "forbidden_paths": [],
    }), encoding="utf-8")
    _git(repo, "add", ".joao-profile.json")
    _git(repo, "commit", "-qm", "profile")

    def build(_run, workspace, _folder):
        (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        return {"ok": True}

    class Reviewer:
        provider = "codex"
        model = "fixture"
        def review(self, run, _folder):
            return {"ok": True, "decision": "pass", "returncode": 0,
                    "proof": {"candidate_tree": run.get("candidate_tree"),
                              "verdict": "ACCEPT", "findings": [],
                              "reviewer": {"provider": "ignored", "model": "ignored"}}}

    runtime = RunRuntime(
        tmp_path / "runtime-state", builder=SandboxBuilder(build),
        reviewer=Reviewer(), profiles=LocalProfileAdapter(),
    )
    run_id = runtime.start(
        project_id="m5runtime", workspace=repo, mission="change app",
        targeted_tests=[], full_tests=[[os.sys.executable, "-c", "pass"]],
    )
    state = runtime.run_once(run_id)
    assert state["status"] == "needs_approval"
    candidate = state["candidate"]
    assert candidate["identity_v2"]["schema_version"] == 2
    review = json.loads((runtime._dir(run_id) / "review-evidence.json").read_text())
    assert review["proof"]["identity_digest"] == candidate["identity_digest"]
    assert review["proof"]["review_signature"]

    approved = runtime.approve(run_id)
    assert approved["status"] == "accepted"
    approval = json.loads((runtime._dir(run_id) / "approval-record.json").read_text())
    assert approval["identity_digest"] == candidate["identity_digest"]
    assert approval["approval_signature"]

def test_real_promote_rejects_legacy_unsigned_candidate(tmp_path):
    repo = _repo(tmp_path)
    parent = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    tree = _git(repo, "write-tree").stdout.strip()
    commit = _git(repo, "commit-tree", tree, "-p", parent, "-m", "legacy candidate").stdout.strip()
    _git(repo, "reset", "--hard", parent)

    run_dir = tmp_path / "legacy-run"
    run_dir.mkdir()
    review = {
        "proof": {
            "candidate_tree": tree,
            "verdict": "ACCEPT",
            "findings": [],
        }
    }
    (run_dir / "review-evidence.json").write_text(
        json.dumps(review, sort_keys=True), encoding="utf-8"
    )
    review_sha = __import__("hashlib").sha256(
        (run_dir / "review-evidence.json").read_bytes()
    ).hexdigest()
    candidate = {
        "candidate_commit": commit,
        "candidate_tree": tree,
        "parent_commit": parent,
        "readonly_copy": str(tmp_path / "missing"),
    }
    run = {
        "run_id": "legacy-run",
        "status": "accepted",
        "review_verified": True,
        "candidate_tree": tree,
    }
    approval = create_approval_record(
        run, candidate, review_proof_sha256=review_sha
    )
    assert approval["authorizing"] is False
    with pytest.raises(PromotionError, match="legacy/unsigned candidates"):
        promote(
            repo, run_dir, candidate, run["run_id"],
            run=run, approval_record=approval, hmac_key=None,
        )
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == parent
