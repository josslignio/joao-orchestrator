"""Deterministic low-risk continuation policy (V1 — JOSS-455).

Decides whether an APPROVED task is eligible for automatic continuation
(commit + push + PR) based on a strict set of deterministic conditions.

The policy engine decides. GPT/Codex may veto or downgrade to human review
but can never approve or promote risk.

Design invariants:

* Deterministic: same inputs → same decision. No randomness, no LLM judgment.
* Fail closed: if ANY condition is not satisfied, continuation is BLOCKED.
* Self-reference protection: a task must never modify the policy or rules
  that judge that same task.
* Always-human-only: sensitive paths (policy, router, provider config,
  AGENTS.md, auth, permissions, secrets, deps, network, data deletion,
  migrations, spending, publication/merge policy) always require human review.
* No automatic merge.
* No automatic approval of policy changes.
* Standard library only. No network, no subprocess, no shell=True.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from ..domain.identifiers import validate_identifier
from ..storage.atomic import atomic_write_json, append_line

LOW_RISK_SCHEMA_VERSION = 1

# Paths that ALWAYS require human review — a task touching any of these
# can never auto-continue, regardless of other conditions.
ALWAYS_HUMAN_PATHS: Tuple[str, ...] = (
    "policy/",
    "approval_lane",
    "risk_classification",
    "system_prompt",
    "provider_prompt",
    "project.toml",
    "router",
    "provider_config",
    "AGENTS.md",
    "auth",
    "permissions",
    "secrets",
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "Pipfile",
    "Pipfile.lock",
    ".env",
    ".gitignore",
    "migrations/",
    "destroy",
    "delete_user_data",
    "drop_table",
    "truncate",
    "billing",
    "payment",
    "spending",
    "subscription",
)

# File patterns that are forbidden in auto-continuation.
FORBIDDEN_PATTERNS: Tuple[str, ...] = (
    ".env",
    ".git/",
    "venv/",
    ".venv/",
    "node_modules/",
    "__pycache__/",
    ".pyc",
    ".so",
    ".dylib",
    ".dll",
)

# Sensitive file extensions that always require human review.
SENSITIVE_EXTENSIONS: Tuple[str, ...] = (
    ".pem", ".key", ".p12", ".pfx", ".crt", ".cer",
    ".keystore", ".jks",
)


class LowRiskPolicyError(ValueError):
    """Base class for low-risk policy errors."""
    pass


# --------------------------------------------------------------------------- #
# Decision dataclass
# --------------------------------------------------------------------------- #

@dataclass
class LowRiskDecision:
    """The outcome of a low-risk continuation policy evaluation."""
    schema_version: int = LOW_RISK_SCHEMA_VERSION
    task_id: str = ""
    project_id: str = ""
    decision: str = "BLOCKED"  # ALLOW_CONTINUATION | BLOCKED | NEEDS_HUMAN_REVIEW
    reason_code: str = ""
    reason_detail: str = ""
    conditions_checked: int = 0
    conditions_passed: int = 0
    failed_conditions: List[str] = field(default_factory=list)
    critic_verdict: str = ""
    validation_ok: bool = False
    changed_paths: List[str] = field(default_factory=list)
    has_critical_finding: bool = False
    has_high_finding: bool = False
    has_unexpected_file: bool = False
    has_symlink: bool = False
    has_secret: bool = False
    has_forbidden_path: bool = False
    has_out_of_policy_path: bool = False
    self_reference_violation: bool = False
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW_CONTINUATION"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"))


def _is_symlink(path_str: str) -> bool:
    """Check if a path is a symlink (best-effort, no filesystem access needed
    for string patterns; actual symlink check done by caller)."""
    return path_str.startswith("->") or "symlink" in path_str.lower()


def _matches_any(path: str, patterns: Tuple[str, ...]) -> bool:
    """Check if a path matches any pattern (substring match)."""
    lower = path.lower()
    for pat in patterns:
        if pat.lower() in lower:
            return True
    return False


def _matches_sensitive_extension(path: str) -> bool:
    lower = path.lower()
    for ext in SENSITIVE_EXTENSIONS:
        if lower.endswith(ext):
            return True
    return False


# --------------------------------------------------------------------------- #
# Low-risk policy engine
# --------------------------------------------------------------------------- #

class LowRiskPolicyEngine:
    """Deterministic policy engine for low-risk task continuation.

    Evaluates a task against strict conditions and returns a decision:
    ALLOW_CONTINUATION, BLOCKED, or NEEDS_HUMAN_REVIEW.

    The engine is purely deterministic — no LLM, no network, no subprocess.
    """

    def __init__(self, state_root: Path, now_fn=None):
        self.state_root = Path(state_root).expanduser().resolve()
        self._now_fn = now_fn or _now_iso

    # -- path helpers ---------------------------------------------------- #

    def _policy_dir(self, project_id: str, task_id: str) -> Path:
        validate_identifier(project_id, "project_id")
        validate_identifier(task_id, "task_id")
        return (self.state_root / "tasks" / project_id / task_id
                / "low_risk_policy")

    def _decision_path(self, project_id: str, task_id: str) -> Path:
        return self._policy_dir(project_id, task_id) / "low_risk_decision.json"

    def _audit_path(self, project_id: str, task_id: str) -> Path:
        return self._policy_dir(project_id, task_id) / "low_risk_audit.jsonl"

    # -- core evaluation -------------------------------------------------- #

    def evaluate(
        self,
        task_id: str,
        project_id: str,
        pre_authorized: bool,
        validation_ok: bool,
        critic_verdict: str,
        changed_paths: List[str],
        allowed_write_paths: List[str],
        forbidden_paths: List[str],
        patch_sha256: str = "",
        artifact_sha256: str = "",
        findings: Optional[List[dict]] = None,
        has_secret: bool = False,
        self_reference: bool = False,
    ) -> LowRiskDecision:
        """Evaluate whether a task is eligible for automatic continuation.

        All conditions must be satisfied for ALLOW_CONTINUATION. Any failure
        results in BLOCKED or NEEDS_HUMAN_REVIEW.

        Args:
            task_id: The task being evaluated.
            project_id: The project the task belongs to.
            pre_authorized: Whether the task was explicitly pre-authorized.
            validation_ok: Whether deterministic validation passed.
            critic_verdict: The critic's final verdict (PASS/FAIL/FIX).
            changed_paths: List of file paths changed by the task.
            allowed_write_paths: Paths the task is allowed to write to.
            forbidden_paths: Paths the task is forbidden from touching.
            patch_sha256: SHA-256 of the patch.
            artifact_sha256: SHA-256 of the artifact set.
            findings: Review findings (list of dicts with severity).
            has_secret: Whether a secret was detected.
            self_reference: Whether the task modifies policy/rules that
                judge itself (self-reference protection).

        Returns:
            LowRiskDecision with the outcome.
        """
        decision = LowRiskDecision(
            task_id=task_id,
            project_id=project_id,
            critic_verdict=critic_verdict,
            validation_ok=validation_ok,
            changed_paths=list(changed_paths),
            has_secret=has_secret,
            created_at=self._now_fn(),
        )

        findings = findings or []
        conditions: List[Tuple[str, bool, str]] = []

        # 1. Task was explicitly pre-authorized.
        conditions.append((
            "pre_authorized", pre_authorized,
            "task was not explicitly pre-authorized"))

        # 2. Deterministic validation passed.
        conditions.append((
            "validation_passed", validation_ok,
            "deterministic validation did not pass"))

        # 3. Final semantic review is PASS.
        critic_pass = critic_verdict == "PASS"
        conditions.append((
            "critic_pass", critic_pass,
            f"final semantic review is {critic_verdict!r}, not PASS"))

        # 4. Artifact and patch hashes match (non-empty).
        hash_ok = bool(patch_sha256) and bool(artifact_sha256)
        if hash_ok:
            hash_match = patch_sha256 == artifact_sha256
        else:
            # If either is empty, we treat it as "not verified" → fail.
            hash_match = False
        conditions.append((
            "hash_match", hash_match,
            "artifact and patch hashes do not match"))

        # 5. No critical finding.
        has_critical = any(
            f.get("severity") == "critical" for f in findings)
        decision.has_critical_finding = has_critical
        conditions.append((
            "no_critical", not has_critical,
            "critical finding present"))

        # 6. No high finding.
        has_high = any(
            f.get("severity") == "high" for f in findings)
        decision.has_high_finding = has_high
        conditions.append((
            "no_high", not has_high,
            "high finding present"))

        # 7. No unexpected file (all changed paths within allowed_write_paths).
        unexpected = self._detect_unexpected_files(
            changed_paths, allowed_write_paths)
        decision.has_unexpected_file = bool(unexpected)
        conditions.append((
            "no_unexpected_file", not unexpected,
            f"unexpected files: {unexpected}"))

        # 8. No symlink.
        symlinks = [p for p in changed_paths if _is_symlink(p)]
        decision.has_symlink = bool(symlinks)
        conditions.append((
            "no_symlink", not symlinks,
            f"symlinks detected: {symlinks}"))

        # 9. No secret.
        conditions.append((
            "no_secret", not has_secret,
            "secret detected"))

        # 10. No forbidden path.
        forbidden_hits = [p for p in changed_paths
                          if _matches_any(p, tuple(forbidden_paths))]
        decision.has_forbidden_path = bool(forbidden_hits)
        conditions.append((
            "no_forbidden_path", not forbidden_hits,
            f"forbidden paths: {forbidden_hits}"))

        # 11. No out-of-policy path (sensitive extensions/patterns).
        out_of_policy = self._detect_out_of_policy(changed_paths)
        decision.has_out_of_policy_path = bool(out_of_policy)
        conditions.append((
            "no_out_of_policy_path", not out_of_policy,
            f"out-of-policy paths: {out_of_policy}"))

        # 12. No package installation (check for dependency files).
        dep_files = [p for p in changed_paths
                     if _matches_any(p, ALWAYS_HUMAN_PATHS)]
        conditions.append((
            "no_package_installation", not dep_files,
            f"dependency/config files: {dep_files}"))

        # 13. No destructive Git operation (checked by command policy, but
        # we also verify no .git path changes).
        git_changes = [p for p in changed_paths if ".git/" in p]
        conditions.append((
            "no_destructive_git", not git_changes,
            f"git path changes: {git_changes}"))

        # 14. No authentication change.
        auth_changes = [p for p in changed_paths
                        if _matches_any(p, ("auth", "permissions", "secrets"))]
        conditions.append((
            "no_auth_change", not auth_changes,
            f"auth/permission changes: {auth_changes}"))

        # 15. No permission expansion (covered by auth check + policy check).
        policy_changes = [p for p in changed_paths
                          if _matches_any(p, ("policy", "approval_lane",
                                              "risk_classification"))]
        conditions.append((
            "no_permission_expansion", not policy_changes,
            f"policy changes: {policy_changes}"))

        # 16. No external spending (cannot detect deterministically, but
        # we check for billing/payment paths).
        spending_changes = [p for p in changed_paths
                            if _matches_any(p, ("billing", "payment",
                                                "spending", "subscription"))]
        conditions.append((
            "no_external_spending", not spending_changes,
            f"spending-related changes: {spending_changes}"))

        # 17. No deletion of user data.
        deletion_changes = [p for p in changed_paths
                            if _matches_any(p, ("destroy", "delete_user_data",
                                                "drop_table", "truncate"))]
        conditions.append((
            "no_data_deletion", not deletion_changes,
            f"data deletion changes: {deletion_changes}"))

        # 18. Self-reference protection.
        decision.self_reference_violation = self_reference
        conditions.append((
            "no_self_reference", not self_reference,
            "task modifies policy/rules that judge itself"))

        # 19. No always-human-only paths.
        human_paths = [p for p in changed_paths
                       if _matches_any(p, ALWAYS_HUMAN_PATHS)]
        conditions.append((
            "no_always_human_path", not human_paths,
            f"always-human-only paths: {human_paths}"))

        # Evaluate all conditions.
        decision.conditions_checked = len(conditions)
        failed = []
        for name, passed, reason in conditions:
            if passed:
                decision.conditions_passed += 1
            else:
                failed.append(name)

        decision.failed_conditions = failed

        # Determine the decision.
        if not failed:
            decision.decision = "ALLOW_CONTINUATION"
            decision.reason_code = "all_conditions_satisfied"
        elif decision.self_reference_violation:
            decision.decision = "BLOCKED"
            decision.reason_code = "self_reference_protection"
            decision.reason_detail = (
                "a task must never modify the policy or rules that judge "
                "that same task")
        elif human_paths or policy_changes or auth_changes:
            decision.decision = "NEEDS_HUMAN_REVIEW"
            decision.reason_code = "sensitive_path"
            decision.reason_detail = (
                "task touches always-human-only paths")
        else:
            decision.decision = "BLOCKED"
            decision.reason_code = "condition_failed"
            decision.reason_detail = (
                f"failed conditions: {failed}")

        return decision

    def _detect_unexpected_files(
        self,
        changed_paths: List[str],
        allowed_write_paths: List[str],
    ) -> List[str]:
        """Detect files outside the allowed write paths."""
        if not allowed_write_paths:
            return list(changed_paths)  # All unexpected if no allowed paths.
        unexpected = []
        for path in changed_paths:
            # Check if path is within any allowed write path.
            allowed = False
            for allowed_path in allowed_write_paths:
                if path.startswith(allowed_path) or allowed_path == ".":
                    allowed = True
                    break
            if not allowed:
                unexpected.append(path)
        return unexpected

    def _detect_out_of_policy(self, changed_paths: List[str]) -> List[str]:
        """Detect paths with sensitive extensions or forbidden patterns."""
        out = []
        for path in changed_paths:
            if _matches_sensitive_extension(path):
                out.append(path)
            elif _matches_any(path, FORBIDDEN_PATTERNS):
                out.append(path)
        return out

    # -- persistence ------------------------------------------------------ #

    def persist(self, decision: LowRiskDecision) -> str:
        """Persist the decision atomically and append to audit log."""
        d_dir = self._policy_dir(decision.project_id, decision.task_id)
        d_dir.mkdir(parents=True, exist_ok=True)

        atomic_write_json(self._decision_path(decision.project_id,
                                               decision.task_id),
                          decision.to_dict())
        append_line(self._audit_path(decision.project_id, decision.task_id),
                    json.dumps(decision.to_dict(), sort_keys=True,
                               ensure_ascii=False))
        return str(self._decision_path(decision.project_id, decision.task_id))

    def load_decision(self, project_id: str, task_id: str) -> Optional[LowRiskDecision]:
        """Load a previously persisted decision."""
        path = self._decision_path(project_id, task_id)
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return LowRiskDecision(**data)

    def evaluate_and_persist(self, **kwargs) -> LowRiskDecision:
        """Evaluate and persist in one call."""
        decision = self.evaluate(**kwargs)
        self.persist(decision)
        return decision
