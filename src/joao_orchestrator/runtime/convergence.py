"""Bounded review/fix convergence engine.

Automates the cycle: semantic review → structured verdict → optional
single targeted fix → re-validation → final review.  At most one fixer
dispatch.  No unbounded loops.  No network.  No real provider invocation
in tests (fake reviewer/fixer executables only).

All artifacts are persisted outside managed repositories via TaskStore.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..domain.events import make_event, now_iso
from ..domain.models import TaskMeta, TaskState, ProjectProfile, ValidationRun
from ..policy.paths import detect_path_violations, escapes_root
from ..providers.budget import BudgetStore, ProviderBudget
from ..providers.router import RoutingRequest, route
from ..storage.atomic import atomic_write_json, atomic_write_text


# ---------------------------------------------------------------------------
# Review verdict schema
# ---------------------------------------------------------------------------

class ReviewVerdict(str, Enum):
    PASS = "PASS"
    FIX_REQUIRED = "FIX_REQUIRED"
    BLOCKED = "BLOCKED"


class FindingSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingCategory(str, Enum):
    CORRECTNESS = "correctness"
    SECURITY = "security"
    ARCHITECTURE = "architecture"
    TESTS = "tests"
    POLICY = "policy"
    MAINTAINABILITY = "maintainability"


@dataclass
class ReviewFinding:
    severity: str
    category: str
    path: str
    line: Optional[int] = None
    evidence: str = ""
    required_change: str = ""

    def to_dict(self) -> dict:
        d = {
            "severity": self.severity,
            "category": self.category,
            "path": self.path,
        }
        if self.line is not None:
            d["line"] = self.line
        d["evidence"] = self.evidence
        d["required_change"] = self.required_change
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewFinding":
        return cls(
            severity=d.get("severity", "medium"),
            category=d.get("category", "correctness"),
            path=d.get("path", ""),
            line=d.get("line"),
            evidence=d.get("evidence", ""),
            required_change=d.get("required_change", ""),
        )


@dataclass
class ReviewResult:
    schema_version: int = 1
    verdict: str = "PASS"
    reason_code: str = ""
    summary: str = ""
    findings: List[dict] = field(default_factory=list)
    requirements_checked: List[str] = field(default_factory=list)
    residual_risks: List[str] = field(default_factory=list)
    reviewed_patch_sha256: str = ""
    provider: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "findings": self.findings,
            "requirements_checked": self.requirements_checked,
            "residual_risks": self.residual_risks,
            "reviewed_patch_sha256": self.reviewed_patch_sha256,
            "provider": self.provider,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewResult":
        findings = []
        for f in d.get("findings", []):
            if isinstance(f, dict):
                findings.append(ReviewFinding.from_dict(f).to_dict())
            else:
                findings.append(f)
        return cls(
            schema_version=int(d.get("schema_version", 1)),
            verdict=d.get("verdict", "PASS"),
            reason_code=d.get("reason_code", ""),
            summary=d.get("summary", ""),
            findings=findings,
            requirements_checked=d.get("requirements_checked", []),
            residual_risks=d.get("residual_risks", []),
            reviewed_patch_sha256=d.get("reviewed_patch_sha256", ""),
            provider=d.get("provider", ""),
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
        )


# ---------------------------------------------------------------------------
# Review context builder
# ---------------------------------------------------------------------------

def build_review_context(
    task: TaskMeta,
    profile: ProjectProfile,
    changed_files: List[str],
    patch_text: str,
    validation_summary: dict,
) -> dict:
    """Build bounded review context. Never includes full repo or secrets."""
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "title": task.title,
        "request": task.request,
        "done_criteria": task.done_criteria or "",
        "allowed_write_paths": list(profile.allowed_write_paths),
        "forbidden_paths": list(profile.forbidden_paths),
        "changed_files": list(changed_files),
        "patch": patch_text,
        "validation_summary": validation_summary,
    }


# ---------------------------------------------------------------------------
# Review JSON validation
# ---------------------------------------------------------------------------

_VALID_VERDICTS = {"PASS", "FIX_REQUIRED", "BLOCKED"}
_VALID_SEVERITIES = {"critical", "high", "medium", "low"}
_VALID_CATEGORIES = {"correctness", "security", "architecture",
                      "tests", "policy", "maintainability"}


def validate_review_json(
    review: dict,
    patch_sha256: str,
    allowed_paths: List[str],
) -> Tuple[Optional[str], Optional[ReviewResult]]:
    """Validate a raw review JSON dict. Returns (error, result).
    On success error is None and result is populated.
    On failure result is None and error describes the problem."""
    if not isinstance(review, dict):
        return "review is not a JSON object", None

    verdict = review.get("verdict", "")
    if verdict not in _VALID_VERDICTS:
        return f"invalid verdict: {verdict!r}", None

    schema_ver = review.get("schema_version", 0)
    if schema_ver != 1:
        return f"unsupported schema_version: {schema_ver}", None

    # Validate findings.
    findings = review.get("findings", [])
    if not isinstance(findings, list):
        return "findings must be a list", None
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            return f"finding[{i}] is not an object", None
        sev = f.get("severity", "")
        if sev not in _VALID_SEVERITIES:
            return f"finding[{i}] invalid severity: {sev!r}", None
        cat = f.get("category", "")
        if cat not in _VALID_CATEGORIES:
            return f"finding[{i}] invalid category: {cat!r}", None
        fpath = f.get("path", "")
        # Reject absolute paths and traversal.
        if fpath.startswith("/") or ".." in fpath:
            return f"finding[{i}] absolute or traversal path: {fpath!r}", None
        # Reject findings outside changed/allowed files.
        if fpath and not _is_relevant_path(fpath, allowed_paths):
            return (f"finding[{i}] path outside allowed/changed files: "
                    f"{fpath!r}"), None

    # Validate patch hash: mandatory field for fail-closed behavior.
    reviewed_hash = review.get("reviewed_patch_sha256", "")
    if not reviewed_hash:
        return "missing required field: reviewed_patch_sha256", None
    if reviewed_hash != patch_sha256:
        return "patch hash mismatch", None

    return None, ReviewResult.from_dict(review)


def _is_relevant_path(path: str, allowed_paths: List[str]) -> bool:
    """Check if a finding path is within an allowed prefix or exact match."""
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    for rule in allowed_paths:
        rule_norm = rule.replace("\\", "/")
        if rule_norm.endswith("/"):
            if norm.startswith(rule_norm) or norm == rule_norm.rstrip("/"):
                return True
        if norm == rule_norm or norm.startswith(rule_norm + "/"):
            return True
        if rule_norm.startswith("*."):
            if norm.rsplit("/", 1)[-1].endswith(rule_norm[1:]):
                return True
    # Exact match on changed file name.
    return norm in allowed_paths


# ---------------------------------------------------------------------------
# Convergence errors
# ---------------------------------------------------------------------------

class ConvergenceError(RuntimeError):
    """Raised when convergence cannot proceed."""


# ---------------------------------------------------------------------------
# Token stripping for child environments
# ---------------------------------------------------------------------------

_PROVIDER_TOKEN_PATTERNS = [
    re.compile(r"(?i)ZAI_"),
    re.compile(r"(?i)ZHIPU_"),
    re.compile(r"(?i)OPENAI_API_KEY"),
    re.compile(r"(?i)ANTHROPIC_API_KEY"),
    re.compile(r"(?i)CLAUDE_.*KEY"),
    re.compile(r"(?i)CLAUDE_.*TOKEN"),
    re.compile(r"(?i)GITHUB_TOKEN"),
    re.compile(r"(?i)GH_TOKEN"),
    re.compile(r"(?i)GH_PAT"),
]


def strip_provider_tokens(env: Dict[str, str]) -> Dict[str, str]:
    """Remove provider and GitHub token variables from env."""
    keys_to_strip = set()
    for k in env:
        for pat in _PROVIDER_TOKEN_PATTERNS:
            if pat.match(k):
                keys_to_strip.add(k)
                break
    return {k: v for k, v in env.items() if k not in keys_to_strip}


# ---------------------------------------------------------------------------
# Fake reviewer/fixer invocation (tests only)
# ---------------------------------------------------------------------------

def _run_fake_reviewer(
    executable: str,
    context: dict,
    worktree_path: Path,
    timeout: int = 60,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[str, int, str]:
    """Run a fake reviewer executable. Returns (stdout, returncode, stderr)."""
    env = env or dict(os.environ)
    env = strip_provider_tokens(env)
    input_json = json.dumps(context, separators=(",", ":"))
    proc = subprocess.run(
        [executable],
        input=input_json,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
        cwd=str(worktree_path),
        env=env,
    )
    return proc.stdout, proc.returncode, proc.stderr


def _run_fake_fixer(
    executable: str,
    fix_request: dict,
    worktree_path: Path,
    timeout: int = 120,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[str, int, str]:
    """Run a fake fixer executable. Returns (stdout, returncode, stderr)."""
    env = env or dict(os.environ)
    env = strip_provider_tokens(env)
    input_json = json.dumps(fix_request, separators=(",", ":"))
    proc = subprocess.run(
        [executable],
        input=input_json,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
        cwd=str(worktree_path),
        env=env,
    )
    return proc.stdout, proc.returncode, proc.stderr


# ---------------------------------------------------------------------------
# Worktree snapshot helpers
# ---------------------------------------------------------------------------

def _snapshot_worktree_files(worktree_path: Path) -> Dict[str, str]:
    """Snapshot all files in the worktree by path → sha256. For mutation detection."""
    snapshot = {}
    wt = Path(worktree_path).resolve()
    if not wt.is_dir():
        return snapshot
    for root, dirs, files in os.walk(wt):
        # Skip .git directories.
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            fpath = Path(root) / f
            try:
                rel = str(fpath.relative_to(wt))
            except ValueError:
                continue
            try:
                digest = hashlib.sha256(fpath.read_bytes()).hexdigest()
                snapshot[rel] = digest
            except (OSError, PermissionError):
                pass
    return snapshot


def _detect_mutations(
    before: Dict[str, str],
    after: Dict[str, str],
) -> Tuple[bool, List[str]]:
    """Compare worktree snapshots. Returns (mutated, list of changes)."""
    changes = []
    # Check for modifications and new files.
    for path, digest in after.items():
        if path not in before:
            changes.append(f"created: {path}")
        elif before[path] != digest:
            changes.append(f"modified: {path}")
    # Check for deleted files.
    for path in before:
        if path not in after:
            changes.append(f"deleted: {path}")
    return bool(changes), changes


# ---------------------------------------------------------------------------
# Convergence engine
# ---------------------------------------------------------------------------

@dataclass
class ConvergenceConfig:
    """Configuration for the convergence engine."""
    max_fixes: int = 1
    review_engine: str = "auto"
    fix_engine: str = "auto"
    review_executable: Optional[str] = None
    fix_executable: Optional[str] = None
    review_timeout: int = 60
    fix_timeout: int = 120
    glm_adapter: str = "~/.local/bin/joao-glm"
    now_fn: Any = None  # Injectable for tests.

    def now(self) -> str:
        if self.now_fn:
            return self.now_fn()
        return now_iso()


@dataclass
class ConvergenceResult:
    """Outcome of the convergence cycle."""
    ok: bool = False
    final_state: str = ""
    verdict: str = ""
    reason_code: str = ""
    reviewer_calls: int = 0
    fixer_calls: int = 0
    convergence_already_done: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "final_state": self.final_state,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "reviewer_calls": self.reviewer_calls,
            "fixer_calls": self.fixer_calls,
            "convergence_already_done": self.convergence_already_done,
            "error": self.error,
        }


def run_convergence(
    task: TaskMeta,
    profile: ProjectProfile,
    store: Any,  # TaskStore
    budget_store: BudgetStore,
    config: ConvergenceConfig,
    worktree_path: Path,
    codex_available: bool = False,
    codex_verified: bool = False,
    opencode_available: bool = False,
    opencode_verified: bool = False,
) -> ConvergenceResult:
    """Execute the bounded convergence cycle.

    Flow:
      AWAITING_APPROVAL → REVIEWING → AWAITING_APPROVAL  (PASS)
    or:
      AWAITING_APPROVAL → REVIEWING → FIXING → VALIDATING →
        REVIEWING → AWAITING_APPROVAL  (fix then final PASS)
    or:
      Any failure → FAILED
    """
    # --- Pre-checks ---
    if task.state != TaskState.AWAITING_APPROVAL.value:
        return ConvergenceResult(
            error=f"task not in AWAITING_APPROVAL (current: {task.state})")
    if is_terminal(task.state):
        return ConvergenceResult(
            error=f"task is terminal ({task.state})")

    # Check if convergence already completed successfully.
    convergence_art = store.artifact_path(
        task.project_id, task.task_id, "convergence_result.json")
    if convergence_art.is_file():
        try:
            prev = json.loads(convergence_art.read_text(encoding="utf-8"))
            if prev.get("ok"):
                return ConvergenceResult(
                    ok=False,
                    convergence_already_done=True,
                    error="convergence already succeeded for this task")
        except (json.JSONDecodeError, OSError):
            pass

    # Load required artifacts.
    load_err = _load_required_artifacts(
        task.project_id, task.task_id, store)
    if load_err:
        return ConvergenceResult(error=load_err)

    task_root = store.task_directory(task.project_id, task.task_id)

    # Load artifact data.
    changed_files_data = json.loads(
        (task_root / "changed_files.json").read_text(encoding="utf-8"))
    changed_files = changed_files_data.get("changed_files", [])

    patch_path = task_root / "patch.diff"
    patch_text = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
    if not patch_text:
        # No changes captured — patch is empty but valid.
        pass
    patch_sha256 = hashlib.sha256(patch_text.encode()).hexdigest()

    validation_data = json.loads(
        (task_root / "validation.json").read_text(encoding="utf-8"))

    # Load worktree metadata.
    wt_meta_path = task_root / "worktree.json"
    wt_meta = json.loads(wt_meta_path.read_text(encoding="utf-8")) \
        if wt_meta_path.is_file() else {}

    wt = Path(worktree_path).resolve()

    # ---- Stage 1: Route review provider ----
    review_routing = _route_provider(
        store, budget_store, task, config,
        "review", "codex-subscription",
        codex_available, codex_verified,
        opencode_available, opencode_verified,
    )
    if review_routing is None:
        _fail(store, task, "no usable review provider")
        return ConvergenceResult(
            error="no usable review provider",
            final_state="FAILED")

    # ---- Stage 2: Build review context ----
    review_context = build_review_context(
        task=task,
        profile=profile,
        changed_files=changed_files,
        patch_text=patch_text,
        validation_summary={
            "ok": validation_data.get("ok", False),
            "commands_run": len(validation_data.get("commands", [])),
            "violations": validation_data.get("violations", []),
        },
    )

    # ---- Stage 3: Snapshot worktree before review ----
    before_review = _snapshot_worktree_files(wt)

    # ---- Stage 4: Run reviewer ----
    store.transition(task.project_id, task.task_id,
                     TaskState.REVIEWING.value,
                     reason="convergence review started")

    review_started = config.now()
    store.write_artifact_json(task.project_id, task.task_id,
                              "initial_review_request.json",
                              {
                                  "context": review_context,
                                  "provider": review_routing.get("selected_provider"),
                                  "started_at": review_started,
                              })

    review_result = _invoke_reviewer(
        review_routing, config, review_context, wt, profile)

    review_finished = config.now()
    review_result.started_at = review_started
    review_result.finished_at = review_finished
    review_result.provider = review_routing.get("selected_provider", "")

    # Persist initial review result (before we overwrite the hash).
    store.write_artifact_json(task.project_id, task.task_id,
                              "initial_review_result.json",
                              review_result.to_dict())

    # ---- Stage 5: Check worktree mutation ----
    after_review = _snapshot_worktree_files(wt)
    mutated, mutations = _detect_mutations(before_review, after_review)
    if mutated:
        _fail(store, task,
              f"reviewer mutated worktree: {mutations}")
        return ConvergenceResult(
            error=f"reviewer mutated worktree: {mutations}",
            final_state="FAILED",
            reviewer_calls=1)

    # ---- Stage 6: Validate review JSON ----
    allowed = list(profile.allowed_write_paths) + list(changed_files)
    val_err, parsed = validate_review_json(
        review_result.to_dict(), patch_sha256, allowed)
    if val_err:
        _fail(store, task, f"review JSON invalid: {val_err}")
        return ConvergenceResult(
            error=f"review JSON invalid: {val_err}",
            final_state="FAILED",
            reviewer_calls=1)

    # ---- Stage 7: Handle verdict ----
    if parsed.verdict == ReviewVerdict.PASS.value:
        # PASS: no fixer invocation.
        _convergence_success(store, task)
        store.write_artifact_json(task.project_id, task.task_id,
                                  "convergence_result.json",
                                  ConvergenceResult(
                                      ok=True,
                                      final_state=TaskState.AWAITING_APPROVAL.value,
                                      verdict="PASS",
                                      reviewer_calls=1,
                                      fixer_calls=0,
                                  ).to_dict())
        return ConvergenceResult(
            ok=True,
            final_state=TaskState.AWAITING_APPROVAL.value,
            verdict="PASS",
            reviewer_calls=1,
            fixer_calls=0)

    if parsed.verdict == ReviewVerdict.BLOCKED.value:
        _fail(store, task, f"review BLOCKED: {parsed.summary}")
        return ConvergenceResult(
            ok=False,
            final_state="FAILED",
            verdict="BLOCKED",
            reason_code=parsed.reason_code,
            reviewer_calls=1,
            fixer_calls=0)

    # FIX_REQUIRED
    if config.max_fixes < 1:
        _fail(store, task,
              "FIX_REQUIRED but max-fixes=0; cannot fix")
        return ConvergenceResult(
            ok=False,
            final_state="FAILED",
            verdict="FIX_REQUIRED",
            reason_code=parsed.reason_code,
            error="FIX_REQUIRED but max-fixes=0",
            reviewer_calls=1,
            fixer_calls=0)

    # ---- Stage 8: Route fix provider ----
    fix_routing = _route_provider(
        store, budget_store, task, config,
        "fix", "opencode-zai",
        codex_available, codex_verified,
        opencode_available, opencode_verified,
    )
    if fix_routing is None:
        _fail(store, task, "no usable fix provider")
        return ConvergenceResult(
            ok=False,
            final_state="FAILED",
            error="no usable fix provider",
            reviewer_calls=1,
            fixer_calls=0)

    # ---- Stage 9: Transition to FIXING ----
    store.transition(task.project_id, task.task_id,
                     TaskState.FIXING.value,
                     reason="dispatching fixer")

    # ---- Stage 10: Build fix request ----
    fix_request = {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "request": task.request,
        "done_criteria": task.done_criteria or "",
        "findings": parsed.findings,
        "current_patch": patch_text,
        "allowed_write_paths": list(profile.allowed_write_paths),
        "forbidden_paths": list(profile.forbidden_paths),
    }

    store.write_artifact_json(task.project_id, task.task_id,
                              "fix_request.json", fix_request)

    # ---- Stage 11: Run fixer ----
    fix_result = _invoke_fixer(
        fix_routing, config, fix_request, wt, profile)

    store.write_artifact_json(task.project_id, task.task_id,
                              "fix_dispatch_result.json",
                              {"returncode": fix_result[1],
                               "stdout": fix_result[0][:4096],
                               "stderr": fix_result[2][:4096],
                               "provider": fix_routing.get("selected_provider")})

    # ---- Stage 12: Post-fix path enforcement ----
    post_changed = _capture_changed_files(wt)
    violations = _check_post_fix_paths(
        post_changed, before_review, profile)
    if violations:
        _fail(store, task,
              f"fixer path violation: {violations}")
        _persist_post_fix(store, task, post_changed, wt, violations)
        return ConvergenceResult(
            ok=False,
            final_state="FAILED",
            error=f"fixer path violation: {violations}",
            reviewer_calls=1,
            fixer_calls=1)

    # Check for unexpected files (created by fixer outside allowed).
    unexpected = _detect_unexpected_files(
        post_changed, before_review, list(profile.allowed_write_paths))
    if unexpected:
        _fail(store, task,
              f"fixer created unexpected files: {unexpected}")
        _persist_post_fix(store, task, post_changed, wt,
                          [f"unexpected: {u}" for u in unexpected])
        return ConvergenceResult(
            ok=False,
            final_state="FAILED",
            error=f"fixer created unexpected files: {unexpected}",
            reviewer_calls=1,
            fixer_calls=1)

    # Check for symlinks.
    symlinks = _detect_symlinks(wt)
    if symlinks:
        _fail(store, task,
              f"fixer created symlinks: {symlinks}")
        return ConvergenceResult(
            ok=False,
            final_state="FAILED",
            error=f"fixer created symlinks: {symlinks}",
            reviewer_calls=1,
            fixer_calls=1)

    # ---- Stage 13: Capture post-fix artifacts ----
    new_patch = _capture_patch(wt)
    store.write_artifact_json(task.project_id, task.task_id,
                              "post_fix_changed_files.json",
                              {"changed_files": post_changed})
    if new_patch:
        store.write_artifact_text(task.project_id, task.task_id,
                                  "post_fix_patch.diff", new_patch)

    # ---- Stage 14: Post-fix validation ----
    store.transition(task.project_id, task.task_id,
                     TaskState.VALIDATING.value,
                     reason="post-fix validation")

    validation_ok, val_result = _run_post_fix_validation(
        store, task, profile, wt)
    store.write_artifact_json(task.project_id, task.task_id,
                              "post_fix_validation.json",
                              val_result)

    if not validation_ok:
        _fail(store, task, "post-fix validation failed")
        return ConvergenceResult(
            ok=False,
            final_state="FAILED",
            error="post-fix validation failed",
            reviewer_calls=1,
            fixer_calls=1)

    # ---- Stage 15: Final review ----
    new_patch_text = new_patch or ""
    new_patch_sha = hashlib.sha256(new_patch_text.encode()).hexdigest()

    final_context = build_review_context(
        task=task,
        profile=profile,
        changed_files=post_changed,
        patch_text=new_patch_text,
        validation_summary={
            "ok": True,
            "commands_run": val_result.get("commands_run", 0),
        },
    )

    before_final_review = _snapshot_worktree_files(wt)

    store.transition(task.project_id, task.task_id,
                     TaskState.REVIEWING.value,
                     reason="final convergence review")

    store.write_artifact_json(task.project_id, task.task_id,
                              "final_review_request.json",
                              {
                                  "context": final_context,
                                  "provider": review_routing.get("selected_provider"),
                                  "started_at": config.now(),
                              })

    final_review_result = _invoke_reviewer(
        review_routing, config, final_context, wt, profile)

    after_final_review = _snapshot_worktree_files(wt)
    mutated2, mutations2 = _detect_mutations(
        before_final_review, after_final_review)
    if mutated2:
        _fail(store, task,
              f"final reviewer mutated worktree: {mutations2}")
        store.write_artifact_json(task.project_id, task.task_id,
                                  "final_review_result.json",
                                  final_review_result.to_dict())
        return ConvergenceResult(
            ok=False,
            final_state="FAILED",
            error=f"final reviewer mutated worktree: {mutations2}",
            reviewer_calls=2,
            fixer_calls=1)

    final_review_result.provider = review_routing.get("selected_provider", "")
    final_review_result.started_at = config.now()
    final_review_result.finished_at = config.now()

    # Validate final review JSON (before overwriting the hash).
    final_allowed = list(profile.allowed_write_paths) + list(post_changed)
    val_err2, parsed2 = validate_review_json(
        final_review_result.to_dict(), new_patch_sha, final_allowed)
    if val_err2:
        _fail(store, task,
              f"final review JSON invalid: {val_err2}")
        store.write_artifact_json(task.project_id, task.task_id,
                                  "final_review_result.json",
                                  final_review_result.to_dict())
        return ConvergenceResult(
            ok=False,
            final_state="FAILED",
            error=f"final review JSON invalid: {val_err2}",
            reviewer_calls=2,
            fixer_calls=1)

    store.write_artifact_json(task.project_id, task.task_id,
                              "final_review_result.json",
                              final_review_result.to_dict())

    # ---- Stage 16: Final verdict handling ----
    if parsed2.verdict == ReviewVerdict.PASS.value:
        _convergence_success(store, task)
        store.write_artifact_json(task.project_id, task.task_id,
                                  "convergence_result.json",
                                  ConvergenceResult(
                                      ok=True,
                                      final_state=TaskState.AWAITING_APPROVAL.value,
                                      verdict="PASS",
                                      reviewer_calls=2,
                                      fixer_calls=1,
                                  ).to_dict())
        return ConvergenceResult(
            ok=True,
            final_state=TaskState.AWAITING_APPROVAL.value,
            verdict="PASS",
            reviewer_calls=2,
            fixer_calls=1)

    # Final FIX_REQUIRED or BLOCKED → FAILED
    _fail(store, task,
          f"final review {parsed2.verdict}: {parsed2.summary}")
    store.write_artifact_json(task.project_id, task.task_id,
                              "convergence_result.json",
                              ConvergenceResult(
                                  ok=False,
                                  final_state="FAILED",
                                  verdict=parsed2.verdict,
                                  reason_code=parsed2.reason_code,
                                  reviewer_calls=2,
                                  fixer_calls=1,
                                  error="final review did not pass; human intervention required",
                              ).to_dict())
    return ConvergenceResult(
        ok=False,
        final_state="FAILED",
        verdict=parsed2.verdict,
        reason_code=parsed2.reason_code,
        reviewer_calls=2,
        fixer_calls=1,
        error="final review did not pass; human intervention required")


# ---------------------------------------------------------------------------
# Helper functions (private)
# ---------------------------------------------------------------------------

def _load_required_artifacts(
    project_id: str, task_id: str, store: Any,
) -> Optional[str]:
    """Check that all required artifacts exist. Returns error string or None."""
    required = [
        "state.json", "events.jsonl", "worktree.json",
        "changed_files.json", "validation.json", "review_packet.md",
    ]
    optional = ["dispatch_result.json", "patch.diff"]
    task_root = store.task_directory(project_id, task_id)
    for name in required:
        path = task_root / name
        if not path.is_file():
            return f"missing required artifact: {name}"
    # Validate state.json is parseable.
    try:
        json.loads((task_root / "state.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return f"malformed state.json: {exc}"
    return None


def _route_provider(
    store, budget_store, task, config,
    purpose: str, preferred: str,
    codex_available: bool, codex_verified: bool,
    opencode_available: bool, opencode_verified: bool,
) -> Optional[dict]:
    """Route a provider for review or fix. Returns routing dict or None."""
    if purpose == "fix":
        category = "implementation"
    else:
        category = "review"

    codex_budget = budget_store.load_budget("codex-subscription")
    opencode_budget = budget_store.load_budget("opencode-zai")

    engine = config.fix_engine if purpose == "fix" else config.review_engine
    if engine == "fake":
        # Fake stands in for the preferred provider for routing.
        engine = preferred
    elif engine not in ("auto", "codex-subscription", "opencode-zai"):
        engine = "auto"

    routing_request = RoutingRequest(
        engine=engine,
        task_category=category,
        task_size="small",
        project_id=task.project_id,
        codex_available=codex_available,
        codex_verified=codex_verified,
        opencode_available=opencode_available,
        opencode_verified=opencode_verified,
    )

    result = route(routing_request, codex_budget, opencode_budget)

    if not result.selected_provider:
        budget_store.record_routing_decision(result.to_dict())
        return None

    budget_store.record_routing_decision(result.to_dict())
    return result.to_dict()


def _invoke_reviewer(
    routing: dict, config: ConvergenceConfig,
    context: dict, worktree_path: Path,
    profile: ProjectProfile,
) -> ReviewResult:
    """Invoke a reviewer. Uses fake executable for tests, real for production.
    
    Fail-closed behavior: an unavailable or unimplemented real reviewer returns
    BLOCKED/ERROR, never PASS. Fake behavior remains available only when explicitly
    configured for tests."""
    provider = routing.get("selected_provider", "")
    exe = config.review_executable
    if exe:
        # A fake executable is configured — use it regardless of routed provider.
        stdout, rc, stderr = _run_fake_reviewer(
            exe, context, worktree_path,
            timeout=config.review_timeout,
        )
        try:
            return ReviewResult.from_dict(json.loads(stdout))
        except (json.JSONDecodeError, ValueError):
            return ReviewResult(
                verdict="BLOCKED",
                reason_code="invalid_json",
                summary=f"Reviewer produced invalid JSON: {stderr[:256]}",
                started_at=config.now(),
                finished_at=config.now(),
            )
    if provider == "fake":
        # No executable provided, fake engine default.
        return ReviewResult(
            verdict="PASS",
            reason_code="fake_reviewer_default",
            summary="Fake reviewer default: PASS",
            started_at=config.now(),
            finished_at=config.now(),
        )
    # Real provider (codex-subscription, opencode-zai) not implemented here.
    # Fail-closed: unavailable real reviewer returns BLOCKED/ERROR, never PASS.
    return ReviewResult(
        verdict="BLOCKED",
        reason_code="no_reviewer_implementation",
        summary=f"No reviewer implementation for {provider} — reviewer unavailable or unimplemented",
        started_at=config.now(),
        finished_at=config.now(),
    )


def _invoke_fixer(
    routing: dict, config: ConvergenceConfig,
    fix_request: dict, worktree_path: Path,
    profile: ProjectProfile,
) -> Tuple[str, int, str]:
    """Invoke a fixer. Returns (stdout, returncode, stderr).
    
    Fail-closed behavior: an unavailable or unimplemented real fixer returns
    non-zero failure, never a synthetic success."""
    from ..bubble.write_tier_policy import assert_write_tier_enabled
    assert_write_tier_enabled("convergence._invoke_fixer")
    exe = config.fix_executable
    if exe:
        return _run_fake_fixer(
            exe, fix_request, worktree_path,
            timeout=config.fix_timeout,
        )
    provider = routing.get("selected_provider", "")
    if provider == "fake":
        return ("", 0, "")
    if provider == "opencode-zai":
        adapter = Path(config.glm_adapter).expanduser().resolve()
        if not adapter.is_file() or not os.access(adapter, os.X_OK):
            return (f"Fixer unavailable or unimplemented: GLM adapter unavailable: {adapter}", 1, "")
        try:
            with tempfile.TemporaryDirectory(prefix="joao-fix-") as temp_raw:
                temp = Path(temp_raw)
                task_file = temp / "fix-task.json"
                output_file = temp / "glm-output.jsonl"
                task_file.write_text(
                    json.dumps(fix_request, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                argv = [
                    str(adapter),
                    "--workspace", str(Path(worktree_path).resolve()),
                    "--task-file", str(task_file),
                    "--output", str(output_file),
                    "--mode", "workspace-write",
                    "--budget", "normal",
                ]
                for rule in profile.allowed_write_paths:
                    argv.extend(["--allowed-path", str(rule)])
                proc = subprocess.run(
                    argv, capture_output=True, text=True, shell=False,
                    timeout=config.fix_timeout, cwd=str(Path(worktree_path).resolve()),
                    env=strip_provider_tokens(dict(os.environ)),
                )
                stdout = output_file.read_text(encoding="utf-8") if output_file.is_file() else proc.stdout
                if proc.returncode and not stdout.strip():
                    stdout = f"Fixer unavailable or unimplemented: {proc.stderr.strip()}"
                return (stdout, proc.returncode, proc.stderr)
        except subprocess.TimeoutExpired:
            return ("", 124, "GLM adapter timed out")
        except Exception as exc:
            detail = f"GLM adapter failed closed: {exc}"
            return (f"Fixer unavailable or unimplemented: {detail}", 1, detail)
    # No supported real fixer for any other provider.
    return (f"Fixer unavailable or unimplemented for {provider}", 1, "")


def _capture_changed_files(worktree_path: Path) -> List[str]:
    """Capture changed files in the worktree via git diff."""
    wt = Path(worktree_path).resolve()
    if not wt.is_dir():
        return []
    changed = []
    # Modified and staged.
    for flag in ([], ["--cached"]):
        proc = subprocess.run(
            ["git", "diff", "--name-only", *flag],
            capture_output=True, text=True, shell=False,
            cwd=str(wt), timeout=20,
        )
        if proc.returncode == 0:
            changed.extend(proc.stdout.strip().splitlines())
    # Untracked.
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, shell=False,
        cwd=str(wt), timeout=20,
    )
    if proc.returncode == 0:
        changed.extend(proc.stdout.strip().splitlines())
    return list(dict.fromkeys(p for p in changed if p))


def _capture_patch(worktree_path: Path) -> str:
    """Capture git diff --binary from worktree."""
    wt = Path(worktree_path).resolve()
    if not wt.is_dir():
        return ""
    proc = subprocess.run(
        ["git", "diff", "--binary", "--no-ext-diff", "--no-textconv"],
        capture_output=True, text=True, shell=False,
        cwd=str(wt), timeout=30,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _detect_symlinks(worktree_path: Path) -> List[str]:
    """Detect symlinks in the worktree."""
    wt = Path(worktree_path).resolve()
    symlinks = []
    if not wt.is_dir():
        return symlinks
    for root, dirs, files in os.walk(wt, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            fpath = Path(root) / f
            if fpath.is_symlink():
                try:
                    rel = str(fpath.relative_to(wt))
                    symlinks.append(rel)
                except ValueError:
                    symlinks.append(str(fpath))
    return symlinks


def _check_post_fix_paths(
    post_changed: List[str],
    original_snapshot: Dict[str, str],
    profile: ProjectProfile,
) -> List[str]:
    """Check post-fix paths for forbidden, out-of-policy, or escapes."""
    violations = []
    root = Path(profile.repository_root).resolve()
    for f in post_changed:
        # Forbidden paths.
        reason = _is_forbidden_path(f, profile)
        if reason:
            violations.append(f"forbidden: {f} ({reason})")
            continue
        # Escape root.
        norm = f.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        try:
            (Path(root) / norm).resolve().relative_to(root)
        except ValueError:
            violations.append(f"escapes root: {f}")
    return violations


def _is_forbidden_path(path: str, profile: ProjectProfile) -> str:
    """Check if a path matches any forbidden rule. Returns reason or empty."""
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    name = norm.rsplit("/", 1)[-1]
    for forbidden in profile.forbidden_paths:
        rule = forbidden.replace("\\", "/")
        if rule.startswith("*."):
            if name.endswith(rule[1:]):
                return forbidden
        if rule.endswith("/"):
            if norm == rule.rstrip("/") or norm.startswith(rule):
                return forbidden
        if norm == rule or norm.startswith(rule + "/"):
            return forbidden
    return ""


def _detect_unexpected_files(
    post_changed: List[str],
    original_snapshot: Dict[str, str],
    allowed_paths: List[str],
) -> List[str]:
    """Files created by fixer that are not in allowed paths."""
    unexpected = []
    for f in post_changed:
        if f in original_snapshot:
            continue  # Existed before.
        norm = f.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        allowed = False
        for rule in allowed_paths:
            rule_norm = rule.replace("\\", "/")
            if rule_norm.endswith("/"):
                if norm.startswith(rule_norm) or norm == rule_norm.rstrip("/"):
                    allowed = True
                    break
            if norm == rule_norm or norm.startswith(rule_norm + "/"):
                allowed = True
                break
            if rule_norm.startswith("*."):
                if norm.rsplit("/", 1)[-1].endswith(rule_norm[1:]):
                    allowed = True
                    break
        if not allowed:
            unexpected.append(f)
    return unexpected


def _run_post_fix_validation(
    store, task, profile: ProjectProfile,
    worktree_path: Path,
) -> Tuple[bool, dict]:
    """Run deterministic validation in the worktree after fix."""
    try:
        from ..validation.profiles import load_profile_commands, commands_for_profile
        from ..validation.executor import RestrictedExecutor

        validation_path = Path(profile.repository_root) / ".agent" / "validation.toml"
        commands = load_profile_commands(validation_path, profile)
        selected = commands_for_profile(commands, profile.validation_profile)
        executor = RestrictedExecutor(profile, selected)
        wt_cwd = str(Path(worktree_path).resolve())
        results = []
        for cmd_def in selected:
            argv = [cmd_def.executable] + list(cmd_def.args) + list(cmd_def.args_extra)
            r = executor.run(argv, timeout=cmd_def.timeout, cwd_override=wt_cwd)
            results.append(r.to_dict())
        all_ok = bool(results) and all(r["ok"] for r in results)
        return all_ok, {
            "task_id": task.task_id,
            "ok": all_ok,
            "commands": results,
            "commands_run": len(results),
            "started_at": now_iso(),
            "finished_at": now_iso(),
        }
    except Exception as exc:
        return False, {
            "task_id": task.task_id,
            "ok": False,
            "error": str(exc),
            "commands_run": 0,
        }


def _persist_post_fix(
    store, task, post_changed, worktree_path, violations,
):
    """Persist post-fix artifacts even on failure."""
    store.write_artifact_json(task.project_id, task.task_id,
                              "post_fix_changed_files.json",
                              {"changed_files": post_changed,
                               "violations": violations})
    new_patch = _capture_patch(worktree_path)
    if new_patch:
        store.write_artifact_text(task.project_id, task.task_id,
                                  "post_fix_patch.diff", new_patch)


def _fail(store, task, reason: str) -> None:
    """Transition task to FAILED."""
    if store.can_transition(task.project_id, task.task_id,
                            TaskState.FAILED.value):
        store.transition(task.project_id, task.task_id,
                         TaskState.FAILED.value, reason=reason)


def _convergence_success(store, task) -> None:
    """Transition back to AWAITING_APPROVAL on success."""
    if store.can_transition(task.project_id, task.task_id,
                            TaskState.AWAITING_APPROVAL.value):
        store.transition(task.project_id, task.task_id,
                         TaskState.AWAITING_APPROVAL.value,
                         reason="convergence successful")


def is_terminal(state: str) -> bool:
    """Check if a state is terminal."""
    from .transitions import TERMINAL_STATES
    return state in TERMINAL_STATES
