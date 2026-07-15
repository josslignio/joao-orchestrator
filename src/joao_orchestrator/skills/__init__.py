"""Compact deterministic task skills/templates (T7).

Each skill is a compact, deterministic template — NOT a large prompt and NOT a
permission grant. Skills carry only:
  id/version, categories, required inputs, likely paths, forbidden paths,
  compact context template, implementation checklist, test template, review
  checklist, expected artifacts, stop conditions.

Skills are selected by the plan compiler (T2) based on category/complexity and
render into a compact context packet (T3). The skill itself carries no
permissions and makes no model calls.

Target: median implementation prompt bytes <= 30% baseline.
Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Optional

from ..evaluation.models import sha256_json


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TaskSkill:
    """One compact skill template."""
    skill_id: str
    version: int
    categories: tuple[str, ...]
    required_inputs: tuple[str, ...]
    likely_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    context_template: str       # compact, with {placeholders}
    implementation_checklist: tuple[str, ...]
    test_template: str
    review_checklist: tuple[str, ...]
    expected_artifacts: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "skill_id": self.skill_id,
            "version": self.version,
            "categories": list(self.categories),
            "required_inputs": list(self.required_inputs),
            "likely_paths": list(self.likely_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "context_template": self.context_template,
            "implementation_checklist": list(self.implementation_checklist),
            "test_template": self.test_template,
            "review_checklist": list(self.review_checklist),
            "expected_artifacts": list(self.expected_artifacts),
            "stop_conditions": list(self.stop_conditions),
        }

    def with_integrity(self) -> "TaskSkill":
        return replace(self, version=self.version)  # version is set at defn; hash below

    @property
    def integrity_sha256(self) -> str:
        return sha256_json(self.unsigned_dict())

    def verify_integrity(self, expected_hash: str) -> bool:
        return bool(expected_hash) and expected_hash == self.integrity_sha256

    def to_dict(self) -> dict[str, Any]:
        d = self.unsigned_dict()
        d["integrity_sha256"] = self.integrity_sha256
        return d

    def render_context(self, **placeholders: str) -> str:
        """Render the compact context template with provided placeholders.

        Missing placeholders are left as-is (deterministic). The rendered
        output is intentionally small (target: <= 30% baseline prompt bytes).
        """
        out = self.context_template
        for k, v in placeholders.items():
            out = out.replace("{" + k + "}", str(v))
        return out


# ---------------------------------------------------------------------------
# Initial skill registry (spec list)
# ---------------------------------------------------------------------------

PYTHON_SMALL_FEATURE = TaskSkill(
    skill_id="python-small-feature",
    version=1,
    categories=("implementation",),
    required_inputs=("task_id", "request", "done_criteria", "allowed_paths"),
    likely_paths=("src/{module}.py",),
    forbidden_paths=("policy/", ".env", ".github/workflows/"),
    context_template=(
        "Task: {request}\n"
        "Done: {done_criteria}\n"
        "Allowed: {allowed_paths}\n"
        "Implement one cohesive feature in the allowed files only."
    ),
    implementation_checklist=(
        "parse done criteria",
        "edit allowed files only",
        "no new dependencies",
        "no network calls",
    ),
    test_template=(
        "from {module} import {symbol}\n\n"
        "def test_{symbol}():\n    assert {symbol}() is not None\n"
    ),
    review_checklist=(
        "touches only allowed paths",
        "done criteria verifiable",
        "no secrets",
        "no scope expansion",
    ),
    expected_artifacts=("diff", "test_result"),
    stop_conditions=("done_criteria_met", "review_PASS"),
)

PYTHON_BUG_FIX = TaskSkill(
    skill_id="python-bug-fix",
    version=1,
    categories=("implementation", "tests"),
    required_inputs=("task_id", "request", "failing_test", "allowed_paths"),
    likely_paths=("src/{module}.py", "tests/test_{module}.py"),
    forbidden_paths=("policy/", ".env"),
    context_template=(
        "Bug: {request}\n"
        "Failing test: {failing_test}\n"
        "Fix the root cause in allowed files; do not suppress the test."
    ),
    implementation_checklist=(
        "reproduce via the failing test",
        "locate root cause",
        "minimal fix",
        "test passes",
    ),
    test_template="",
    review_checklist=(
        "root cause addressed not symptom",
        "no test suppression",
        "minimal diff",
    ),
    expected_artifacts=("diff", "test_result"),
    stop_conditions=("failing_test_now_passes", "review_PASS"),
)

ADD_CLI_COMMAND = TaskSkill(
    skill_id="add-cli-command",
    version=1,
    categories=("implementation",),
    required_inputs=("task_id", "command_name", "request", "allowed_paths"),
    likely_paths=("scripts/{script}.py", "src/joao_orchestrator/cli.py"),
    forbidden_paths=("policy/", ".env"),
    context_template=(
        "Add CLI command '{command_name}': {request}\n"
        "Follow the existing argparse subcommand pattern.\n"
    ),
    implementation_checklist=(
        "add subparser",
        "implement handler",
        "wire to existing primitives",
        "add --json output",
    ),
    test_template=(
        "def test_{command_name}_cli():\n"
        "    result = run_cli('{command_name}', '--json')\n"
        "    assert result.returncode == 0\n"
    ),
    review_checklist=(
        "follows existing CLI pattern",
        "exits non-zero on error",
        "no shell=True",
    ),
    expected_artifacts=("diff", "test_result"),
    stop_conditions=("cli_invokable", "review_PASS"),
)

ADD_RUNTIME_ARTIFACT = TaskSkill(
    skill_id="add-runtime-artifact",
    version=1,
    categories=("implementation",),
    required_inputs=("task_id", "request", "allowed_paths"),
    likely_paths=("src/joao_orchestrator/runtime/{module}.py",),
    forbidden_paths=("policy/", ".env"),
    context_template=(
        "Add runtime artifact: {request}\n"
        "Reuse storage/atomic.py and evaluation/models.py patterns.\n"
    ),
    implementation_checklist=(
        "frozen dataclass with integrity",
        "atomic persistence",
        "deterministic serialization",
    ),
    test_template="",
    review_checklist=(
        "follows integrity pattern",
        "atomic writes",
        "deterministic",
    ),
    expected_artifacts=("diff", "test_result"),
    stop_conditions=("artifact_persists", "review_PASS"),
)

ADD_UNIT_TEST = TaskSkill(
    skill_id="add-unit-test",
    version=1,
    categories=("tests",),
    required_inputs=("task_id", "target_module", "allowed_paths"),
    likely_paths=("tests/test_{module}.py",),
    forbidden_paths=("policy/", ".env", "src/"),
    context_template=(
        "Add unit tests for {target_module} in the tests/ directory.\n"
    ),
    implementation_checklist=(
        "cover public surface",
        "deterministic (no network, no clock)",
        "temp dirs only",
    ),
    test_template="",
    review_checklist=(
        "no real state touched",
        "deterministic",
        "covers edge cases",
    ),
    expected_artifacts=("diff", "test_result"),
    stop_conditions=("tests_pass", "review_PASS"),
)

SAFE_GIT_PUBLICATION = TaskSkill(
    skill_id="safe-git-publication",
    version=1,
    categories=("implementation",),
    required_inputs=("task_id", "branch", "files"),
    likely_paths=(),
    forbidden_paths=("policy/", ".env"),
    context_template=(
        "Publish: branch={branch}, files={files}\n"
        "Stage exact files only. Commit. Push without force. Open PR.\n"
    ),
    implementation_checklist=(
        "stage exact files",
        "commit with conventional message",
        "push without force",
        "open PR (no auto-merge)",
    ),
    test_template="",
    review_checklist=(
        "no force push",
        "no auto-merge",
        "exact files only",
    ),
    expected_artifacts=("commit_sha", "pr_url"),
    stop_conditions=("pr_opened", "no_auto_merge"),
)

DOCUMENTATION_ONLY = TaskSkill(
    skill_id="documentation-only",
    version=1,
    categories=("documentation",),
    required_inputs=("task_id", "request", "allowed_paths"),
    likely_paths=("README.md", "docs/{page}.md"),
    forbidden_paths=("src/", "policy/", ".env"),
    context_template=(
        "Docs: {request}\n"
        "Edit markdown in allowed paths only. No code changes.\n"
    ),
    implementation_checklist=(
        "edit markdown only",
        "no code changes",
        "verifiable claims",
    ),
    test_template="",
    review_checklist=(
        "no code touched",
        "claims accurate",
    ),
    expected_artifacts=("diff",),
    stop_conditions=("docs_updated", "review_PASS"),
)

DATA_PIPELINE_STAGE = TaskSkill(
    skill_id="data-pipeline-stage",
    version=1,
    categories=("implementation",),
    required_inputs=("task_id", "stage_name", "request", "allowed_paths"),
    likely_paths=("scripts/{stage}.py",),
    forbidden_paths=("policy/", ".env"),
    context_template=(
        "Pipeline stage '{stage_name}': {request}\n"
        "Deterministic, idempotent, no network unless explicitly authorized.\n"
    ),
    implementation_checklist=(
        "deterministic output",
        "idempotent",
        "atomic artifacts",
    ),
    test_template="",
    review_checklist=(
        "deterministic",
        "idempotent",
        "no hidden state",
    ),
    expected_artifacts=("diff", "test_result"),
    stop_conditions=("stage_runs", "review_PASS"),
)

NO_LOOK_AHEAD_AUDIT = TaskSkill(
    skill_id="no-look-ahead-audit",
    version=1,
    categories=("review",),
    required_inputs=("task_id", "diff", "evaluation_spec"),
    likely_paths=(),
    forbidden_paths=("policy/", ".env"),
    context_template=(
        "Audit (no look-ahead): verify diff against {evaluation_spec}.\n"
        "Only information available BEFORE the task's checkpoint may be used.\n"
    ),
    implementation_checklist=(
        "inspect diff only",
        "no future commits",
        "no peeking at test outcomes beyond the gate",
    ),
    test_template="",
    review_checklist=(
        "no look-ahead",
        "deterministic",
        "integrity-signed",
    ),
    expected_artifacts=("audit_report",),
    stop_conditions=("audit_complete",),
)


# The registry, keyed by skill_id.
SKILLS: dict[str, TaskSkill] = {
    s.skill_id: s for s in (
        PYTHON_SMALL_FEATURE,
        PYTHON_BUG_FIX,
        ADD_CLI_COMMAND,
        ADD_RUNTIME_ARTIFACT,
        ADD_UNIT_TEST,
        SAFE_GIT_PUBLICATION,
        DOCUMENTATION_ONLY,
        DATA_PIPELINE_STAGE,
        NO_LOOK_AHEAD_AUDIT,
    )
}


def get_skill(skill_id: str) -> Optional[TaskSkill]:
    return SKILLS.get(skill_id)


def select_skill(category: str, complexity: str = "SMALL") -> Optional[TaskSkill]:
    """Deterministically select a skill by category + complexity.

    Mapping is deterministic: same (category, complexity) -> same skill.
    Sensitive tasks get no skill (human-only).
    """
    if complexity == "SENSITIVE":
        return None
    cat = (category or "").lower()
    mapping = {
        "documentation": DOCUMENTATION_ONLY,
        "tests": ADD_UNIT_TEST,
        "review": NO_LOOK_AHEAD_AUDIT,
        "implementation": PYTHON_SMALL_FEATURE,
    }
    return mapping.get(cat, PYTHON_SMALL_FEATURE)


def registry_summary() -> dict[str, Any]:
    """A compact summary of the registry (for manifest/audit)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "skill_count": len(SKILLS),
        "skill_ids": sorted(SKILLS.keys()),
        "skills": {sid: {"categories": list(s.categories),
                          "version": s.version,
                          "integrity_sha256": s.integrity_sha256}
                   for sid, s in sorted(SKILLS.items())},
    }
