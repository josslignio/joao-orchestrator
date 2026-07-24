"""Authenticated, one-time activation gate for Run Night Master."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..evaluation.models import sha256_json
from .models import RunNightSpec


class ActivationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(data: dict[str, Any]) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _git(repo_root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if proc.returncode:
        raise ActivationError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout.strip()


@dataclass(frozen=True)
class RunNightActivation:
    activation_id: str
    run_id: str
    spec_sha256: str
    tranche3_sha: str
    runnight_core_sha: str
    sec_boot_sha256: str
    tranche3_evidence_sha256: str
    tranche3_closure_sha256: str
    runnight_evidence_sha256: str
    runnight_closure_sha256: str
    m7_closed: bool
    m8_closed: bool
    m9_closed: bool
    m10_closed: bool
    tranche3_gpt_pass: bool
    runnight_core_gpt_pass: bool
    write_tier_off: bool
    issued_at: str
    expires_at: str
    nonce: str
    key_id: str
    hmac_sha256: str = ""
    schema_version: int = 3

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "activation_id": self.activation_id,
            "run_id": self.run_id,
            "spec_sha256": self.spec_sha256,
            "tranche3_sha": self.tranche3_sha,
            "runnight_core_sha": self.runnight_core_sha,
            "sec_boot_sha256": self.sec_boot_sha256,
            "tranche3_evidence_sha256": self.tranche3_evidence_sha256,
            "tranche3_closure_sha256": self.tranche3_closure_sha256,
            "runnight_evidence_sha256": self.runnight_evidence_sha256,
            "runnight_closure_sha256": self.runnight_closure_sha256,
            "m7_closed": self.m7_closed,
            "m8_closed": self.m8_closed,
            "m9_closed": self.m9_closed,
            "m10_closed": self.m10_closed,
            "tranche3_gpt_pass": self.tranche3_gpt_pass,
            "runnight_core_gpt_pass": self.runnight_core_gpt_pass,
            "write_tier_off": self.write_tier_off,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "key_id": self.key_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunNightActivation":
        required = set(cls.__dataclass_fields__) - {"hmac_sha256", "schema_version"}
        missing = sorted(required - set(data))
        if missing:
            raise ActivationError(f"activation missing fields: {missing}")
        known = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        return cls(**known)

    @classmethod
    def load(cls, path: Path) -> "RunNightActivation":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ActivationError(f"cannot load activation: {exc}") from exc
        if not isinstance(data, dict):
            raise ActivationError("activation must be an object")
        return cls.from_dict(data)

    def sign(self, key: bytes) -> "RunNightActivation":
        signature = hmac.new(
            key, _canonical(self.unsigned_dict()), hashlib.sha256
        ).hexdigest()
        return RunNightActivation(**{**self.__dict__, "hmac_sha256": signature})


def load_hmac_key(path: Path) -> bytes:
    path = Path(path).expanduser().resolve(strict=True)
    if path.is_symlink():
        raise ActivationError("HMAC key file must not be a symlink")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ActivationError("HMAC key file must not be group/world accessible")
    key = path.read_bytes()
    if len(key) < 32:
        raise ActivationError("HMAC key must contain at least 32 bytes")
    return key


def _zip_names(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            if bad:
                raise ActivationError(f"corrupt evidence member: {bad}")
            return archive.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ActivationError(f"invalid evidence ZIP: {exc}") from exc


def _read_zip_bytes(path: Path, filename: str) -> bytes:
    try:
        with zipfile.ZipFile(path) as archive:
            matches = [name for name in archive.namelist() if name.endswith(filename)]
            if len(matches) != 1:
                raise ActivationError(f"{filename} missing or ambiguous in evidence ZIP")
            return archive.read(matches[0])
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise ActivationError(f"invalid evidence ZIP: {exc}") from exc


def _read_zip_text(path: Path, filename: str) -> str:
    try:
        return _read_zip_bytes(path, filename).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ActivationError(f"{filename} is not UTF-8") from exc


def _read_zip_json(path: Path, filename: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_zip_bytes(path, filename))
    except json.JSONDecodeError as exc:
        raise ActivationError(f"invalid JSON in {filename}: {exc}") from exc
    if not isinstance(value, dict):
        raise ActivationError(f"{filename} must contain an object")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActivationError(f"invalid closure JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ActivationError("closure must be an object")
    return value


def verify_tranche3_evidence(
    evidence_zip: Path,
    closure_path: Path,
    *,
    expected_sha: str,
    expected_sec_boot_sha256: str,
) -> dict[str, Any]:
    evidence_zip = Path(evidence_zip).expanduser().resolve(strict=True)
    _zip_names(evidence_zip)

    final_sha = _read_zip_text(evidence_zip, "FINAL_SHA.txt").strip()
    final_tree = _read_zip_text(evidence_zip, "FINAL_TREE.txt").strip()
    failed_gates = _read_zip_text(evidence_zip, "FAILED_GATES.txt").strip()
    sec_base = _read_zip_text(evidence_zip, "SEC_BOOT_BASE_SHA256.txt").strip()
    sec_final = _read_zip_text(evidence_zip, "SEC_BOOT_FINAL_SHA256.txt").strip()
    worktree_status = _read_zip_text(evidence_zip, "WORKTREE_CLEAN.txt")
    worktree_rc = _read_zip_text(evidence_zip, "WORKTREE_CLEAN.exit_code").strip()

    m9 = _read_zip_json(evidence_zip, "M9_CODEX_EXACT_REVIEW.json")
    m10 = _read_zip_json(evidence_zip, "M10_LIVE_VERDICT.json")
    global_comparison = _read_zip_json(evidence_zip, "GLOBAL_COMPARISON.json")
    raw_scan = _read_zip_json(evidence_zip, "RAW_PROVIDER_TEXT_SCAN.json")

    if final_sha != expected_sha:
        raise ActivationError("Tranche 3 final SHA mismatch")
    if len(final_tree) != 40 or any(c not in "0123456789abcdef" for c in final_tree):
        raise ActivationError("Tranche 3 final tree is invalid")
    if failed_gates:
        raise ActivationError("Tranche 3 evidence contains failed gates")
    if sec_base != sec_final or sec_final != expected_sec_boot_sha256:
        raise ActivationError("Tranche 3 SEC-BOOT evidence mismatch")
    if worktree_rc != "0" or worktree_status:
        raise ActivationError("Tranche 3 worktree was not clean")

    if (
        m9.get("pass") is not True
        or m9.get("status") != "ACCEPT"
        or m9.get("candidate_sha") != expected_sha
        or int(m9.get("accepted", 0)) < 1
        or int(m9.get("blocked", 0)) != 0
    ):
        raise ActivationError("M9 exact-SHA evidence is not acceptable")
    if (
        m10.get("pass") is not True
        or m10.get("exact_marker") is not True
        or m10.get("worktree_unchanged") is not True
        or not str(m10.get("selected_provider") or "").strip()
    ):
        raise ActivationError("M10 exact-marker evidence is not acceptable")
    if (
        global_comparison.get("pass") is not True
        or int(global_comparison.get("new_failures_introduced", -1)) != 0
        or global_comparison.get("new_failed_nodes")
        or global_comparison.get("removed_test_nodes")
        or global_comparison.get("changed_failure_signatures")
    ):
        raise ActivationError("Tranche 3 global comparison is not acceptable")
    if (
        raw_scan.get("pass") is not True
        or int(raw_scan.get("raw_provider_text_files", -1)) != 0
        or raw_scan.get("violations")
    ):
        raise ActivationError("raw provider text evidence is not acceptable")

    closure = _read_json(closure_path)
    if (
        closure.get("verdict") != "TRANCHE3_GPT_PASS"
        or closure.get("final_sha") != expected_sha
        or closure.get("final_tree") != final_tree
        or closure.get("evidence_sha256") != sha256_file(evidence_zip)
        or closure.get("zero_open_p0") is not True
        or closure.get("zero_open_p1") is not True
        or closure.get("sec_boot") != "INTACT"
        or closure.get("sec_boot_sha256") != expected_sec_boot_sha256
        or closure.get("write_tier") != "OFF"
    ):
        raise ActivationError("Tranche 3 GPT closure mismatch")

    return {
        "final_sha": final_sha,
        "final_tree": final_tree,
        "evidence_sha256": sha256_file(evidence_zip),
        "m9": "ACCEPT",
        "m10_exact_marker": True,
        "raw_provider_state_files": 0,
    }


def verify_runnight_master_evidence(
    evidence_zip: Path,
    closure_path: Path,
    *,
    expected_base_sha: str,
    expected_core_sha: str,
) -> dict[str, Any]:
    evidence_zip = Path(evidence_zip).expanduser().resolve(strict=True)
    _zip_names(evidence_zip)
    verdict = _read_zip_json(evidence_zip, "RUNNIGHT_MASTER_VERDICT.json")
    failed_gates = _read_zip_text(evidence_zip, "FAILED_GATES.txt").strip()
    final_sha = _read_zip_text(evidence_zip, "FINAL_SHA.txt").strip()
    worktree_status = _read_zip_text(evidence_zip, "WORKTREE_STATUS.txt")

    if failed_gates:
        raise ActivationError("Run Night Master evidence contains failed gates")
    if final_sha != expected_core_sha:
        raise ActivationError("Run Night Master evidence SHA mismatch")
    if worktree_status:
        raise ActivationError("Run Night Master worktree was not clean")
    if (
        verdict.get("verdict") != "AWAITING_GPT_REVIEW"
        or verdict.get("tranche3_base_sha") != expected_base_sha
        or verdict.get("final_sha") != expected_core_sha
        or verdict.get("write_tier") != "OFF"
        or verdict.get("candidate_build_enabled") is not False
    ):
        raise ActivationError("Run Night Master evidence verdict mismatch")

    closure = _read_json(closure_path)
    if (
        closure.get("verdict") != "RUNNIGHT_MASTER_GPT_PASS"
        or closure.get("final_sha") != expected_core_sha
        or closure.get("evidence_sha256") != sha256_file(evidence_zip)
        or closure.get("zero_open_p0") is not True
        or closure.get("zero_open_p1") is not True
        or closure.get("candidate_build_enabled") is not False
        or closure.get("write_tier") != "OFF"
    ):
        raise ActivationError("Run Night GPT closure mismatch")

    return {
        "final_sha": final_sha,
        "evidence_sha256": sha256_file(evidence_zip),
        "candidate_build_enabled": False,
    }


def verify_activation(
    activation: RunNightActivation,
    spec: RunNightSpec,
    *,
    key: bytes,
    sec_boot_relative: str = "src/joao_orchestrator/bubble/write_tier_policy.py",
) -> dict[str, Any]:
    spec.validate()
    if activation.schema_version != 3:
        raise ActivationError("unsupported activation schema")
    expected_hmac = hmac.new(
        key, _canonical(activation.unsigned_dict()), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hmac, activation.hmac_sha256):
        raise ActivationError("activation HMAC mismatch")
    if activation.run_id != spec.run_id or activation.spec_sha256 != spec.spec_sha256:
        raise ActivationError("activation is not bound to this exact spec")
    if activation.runnight_core_sha != spec.authorized_sha:
        raise ActivationError("activation SHA mismatch")
    required = {
        "m7_closed": activation.m7_closed,
        "m8_closed": activation.m8_closed,
        "m9_closed": activation.m9_closed,
        "m10_closed": activation.m10_closed,
        "tranche3_gpt_pass": activation.tranche3_gpt_pass,
        "runnight_core_gpt_pass": activation.runnight_core_gpt_pass,
        "write_tier_off": activation.write_tier_off,
    }
    failed = [name for name, value in required.items() if not value]
    if failed:
        raise ActivationError("activation gate false: " + ", ".join(sorted(failed)))

    now = datetime.now(timezone.utc)
    try:
        issued = datetime.fromisoformat(activation.issued_at.replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(activation.expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ActivationError("invalid activation timestamps") from exc
    if issued.tzinfo is None or expiry.tzinfo is None:
        raise ActivationError("activation timestamps must be timezone-aware")
    if issued > now or expiry <= issued or (expiry - issued).total_seconds() > 86400:
        raise ActivationError("activation validity window is invalid")
    if now >= expiry:
        raise ActivationError("activation expired")

    repo = Path(spec.repo_root).expanduser().resolve()
    state = Path(spec.state_root).expanduser().resolve()
    if repo == state or repo in state.parents or state in repo.parents:
        raise ActivationError("state root and repo must be disjoint")
    if _git(repo, "rev-parse", "HEAD") != activation.runnight_core_sha:
        raise ActivationError("repo HEAD does not match Run Night core SHA")
    if spec.require_clean_repo and _git(repo, "status", "--porcelain=v1"):
        raise ActivationError("repo is not clean")
    sec_boot = repo / sec_boot_relative
    if not sec_boot.is_file() or sha256_file(sec_boot) != activation.sec_boot_sha256:
        raise ActivationError("SEC-BOOT mismatch")

    actual_paths = {
        "tranche3_evidence_sha256": Path(spec.tranche3_evidence_path),
        "tranche3_closure_sha256": Path(spec.tranche3_closure_path),
        "runnight_evidence_sha256": Path(spec.runnight_evidence_path),
        "runnight_closure_sha256": Path(spec.runnight_closure_path),
    }
    for field, path in actual_paths.items():
        if not path.is_file() or sha256_file(path) != getattr(activation, field):
            raise ActivationError(f"{field} does not match the actual file")

    verify_tranche3_evidence(
        Path(spec.tranche3_evidence_path),
        Path(spec.tranche3_closure_path),
        expected_sha=activation.tranche3_sha,
        expected_sec_boot_sha256=activation.sec_boot_sha256,
    )
    verify_runnight_master_evidence(
        Path(spec.runnight_evidence_path),
        Path(spec.runnight_closure_path),
        expected_base_sha=activation.tranche3_sha,
        expected_core_sha=activation.runnight_core_sha,
    )

    consumed = state / "run_night" / "activations" / f"{activation.activation_id}.json"
    if consumed.exists():
        raise ActivationError("activation has already been consumed")
    return {
        "activation_id": activation.activation_id,
        "authorized_sha": activation.runnight_core_sha,
        "sec_boot_sha256": activation.sec_boot_sha256,
        "expires_at": activation.expires_at,
        "consumption_path": str(consumed),
    }


def consume_activation(activation: RunNightActivation, state_root: Path) -> Path:
    path = (
        Path(state_root).expanduser().resolve()
        / "run_night" / "activations" / f"{activation.activation_id}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "schema_version": 3,
            "activation_id": activation.activation_id,
            "run_id": activation.run_id,
            "spec_sha256": activation.spec_sha256,
            "nonce_sha256": hashlib.sha256(activation.nonce.encode()).hexdigest(),
            "consumed_at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
        indent=2,
    ) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ActivationError("activation already consumed") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path
