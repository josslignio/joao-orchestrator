"""Deterministic evaluation harness — versioned data models (C1).

Strict, hashable, content-addressed types describing an evaluation:

  CommandSpec     a single deterministic command to run
  MetricRule      how a metric is interpreted (maximize/minimize/target)
  EvaluationSpec  the full, content-addressed evaluation definition
  CommandResult   observed outcome of one command
  MetricResult    one observed metric value
  EvaluationReport  integrity-signed report for one subject
  KeepDecision    integrity-signed KEEP/REJECT/TIE verdict

Design invariants (see repository AGENTS/invariants):

* Determinism is mandatory: every persisted artifact is serialized via
  ``canonical_json_bytes`` (sorted keys, compact separators, no ASCII escape,
  ``allow_nan=False``) so that SHA-256 integrity hashes are reproducible.
* Integrity is fail-closed: reports/decisions carry an ``integrity_sha256``
  over their *unsigned* payload; tampering is detected on comparison.
* No NaN / non-finite metric values are accepted (``allow_nan=False`` and
  explicit ``math.isfinite`` checks).
* No provider, no network, no package installation, no destructive Git.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from hashlib import sha256
from pathlib import Path
import json
import math
import re
from typing import Any, Mapping


SCHEMA_VERSION = 1
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SECRET_ENV_PREFIXES = (
    "ZAI_", "ZHIPU_", "OPENAI_", "ANTHROPIC_", "CLAUDE_",
    "GITHUB_", "GH_", "GITLAB_",
)
SECRET_ENV_NAMES = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN",
    "GH_TOKEN", "GH_PAT", "GITLAB_TOKEN",
}


def is_secret_env_name(name: str) -> bool:
    upper = name.upper()
    return upper in SECRET_ENV_NAMES or any(
        upper.startswith(prefix) for prefix in SECRET_ENV_PREFIXES
    )


def validate_safe_id(value: str, field_name: str) -> None:
    if not SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} contains unsafe characters: {value!r}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically for hashing and artifact integrity."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


class MetricDirection(str, Enum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"
    TARGET = "target"


class TiePolicy(str, Enum):
    REJECT = "reject"
    KEEP = "keep"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int = 300
    cwd_relative: str = "."
    extra_env: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("command name must be non-empty")
        if not self.argv or not all(isinstance(part, str) and part for part in self.argv):
            raise ValueError(f"{self.name}: argv must be a non-empty string tuple")
        if self.timeout_seconds <= 0:
            raise ValueError(f"{self.name}: timeout_seconds must be > 0")
        cwd_path = Path(self.cwd_relative)
        if cwd_path.is_absolute() or ".." in cwd_path.parts:
            raise ValueError(f"{self.name}: cwd_relative must be a safe relative path")
        for key, value in self.extra_env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(f"{self.name}: extra_env must contain strings only")
            if is_secret_env_name(key):
                raise ValueError(f"{self.name}: secret-like environment key is forbidden: {key}")


@dataclass(frozen=True)
class MetricRule:
    name: str
    direction: MetricDirection
    min_improvement: float = 0.0
    max_regression: float = 0.0
    blocking: bool = True
    target: float | None = None

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("metric name must be non-empty")
        if not math.isfinite(self.min_improvement) or self.min_improvement < 0:
            raise ValueError(f"{self.name}: min_improvement must be finite and >= 0")
        if not math.isfinite(self.max_regression) or self.max_regression < 0:
            raise ValueError(f"{self.name}: max_regression must be finite and >= 0")
        if self.target is not None and not math.isfinite(self.target):
            raise ValueError(f"{self.name}: target must be finite")
        if self.direction is MetricDirection.TARGET and self.target is None:
            raise ValueError(f"{self.name}: target is required for target metrics")
        if self.direction is not MetricDirection.TARGET and self.target is not None:
            raise ValueError(f"{self.name}: target is only valid for target metrics")


@dataclass(frozen=True)
class EvaluationSpec:
    evaluation_id: str
    project_id: str
    commands: tuple[CommandSpec, ...]
    metrics: tuple[MetricRule, ...]
    metrics_file: str = "evaluation_metrics.json"
    required_artifacts: tuple[str, ...] = ()
    tie_policy: TiePolicy = TiePolicy.REJECT
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        validate_safe_id(self.evaluation_id, "evaluation_id")
        validate_safe_id(self.project_id, "project_id")
        metrics_path = Path(self.metrics_file)
        if metrics_path.is_absolute() or ".." in metrics_path.parts:
            raise ValueError("metrics_file must be a safe relative path")
        if not self.commands:
            raise ValueError("at least one command is required")
        names: set[str] = set()
        for command in self.commands:
            command.validate()
            if command.name in names:
                raise ValueError(f"duplicate command name: {command.name}")
            names.add(command.name)
        metric_names: set[str] = set()
        for metric in self.metrics:
            metric.validate()
            if metric.name in metric_names:
                raise ValueError(f"duplicate metric name: {metric.name}")
            metric_names.add(metric.name)
        for artifact in self.required_artifacts:
            path = Path(artifact)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe required artifact path: {artifact}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evaluation_id": self.evaluation_id,
            "project_id": self.project_id,
            "commands": [
                {
                    "name": command.name,
                    "argv": list(command.argv),
                    "timeout_seconds": command.timeout_seconds,
                    "cwd_relative": command.cwd_relative,
                    "extra_env": dict(sorted(command.extra_env.items())),
                }
                for command in self.commands
            ],
            "metrics": [
                {
                    "name": metric.name,
                    "direction": metric.direction.value,
                    "min_improvement": metric.min_improvement,
                    "max_regression": metric.max_regression,
                    "blocking": metric.blocking,
                    "target": metric.target,
                }
                for metric in self.metrics
            ],
            "metrics_file": self.metrics_file,
            "required_artifacts": list(self.required_artifacts),
            "tie_policy": self.tie_policy.value,
        }

    @property
    def sha256(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True)
class CommandResult:
    name: str
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout_tail: str
    stderr_tail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


@dataclass(frozen=True)
class MetricResult:
    name: str
    value: float
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "source": self.source}


@dataclass(frozen=True)
class EvaluationReport:
    subject_label: str
    revision: str
    spec_sha256: str
    command_results: tuple[CommandResult, ...]
    metrics: tuple[MetricResult, ...]
    required_artifacts_ok: bool
    missing_artifacts: tuple[str, ...]
    passed: bool
    reasons: tuple[str, ...]
    created_at: str
    schema_version: int = SCHEMA_VERSION
    integrity_sha256: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "subject_label": self.subject_label,
            "revision": self.revision,
            "spec_sha256": self.spec_sha256,
            "command_results": [item.to_dict() for item in self.command_results],
            "metrics": [item.to_dict() for item in self.metrics],
            "required_artifacts_ok": self.required_artifacts_ok,
            "missing_artifacts": list(self.missing_artifacts),
            "passed": self.passed,
            "reasons": list(self.reasons),
            "created_at": self.created_at,
        }

    def with_integrity(self) -> "EvaluationReport":
        digest = sha256_json(self.unsigned_dict())
        return replace(self, integrity_sha256=digest)

    def verify_integrity(self) -> bool:
        return bool(self.integrity_sha256) and self.integrity_sha256 == sha256_json(
            self.unsigned_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["integrity_sha256"] = self.integrity_sha256
        return payload


class KeepVerdict(str, Enum):
    KEEP = "KEEP"
    REJECT = "REJECT"
    TIE = "TIE"


@dataclass(frozen=True)
class KeepDecision:
    verdict: KeepVerdict
    keep: bool
    reasons: tuple[str, ...]
    improvements: tuple[str, ...]
    regressions: tuple[str, ...]
    blocking_regressions: tuple[str, ...]
    baseline_report_sha256: str
    candidate_report_sha256: str
    spec_sha256: str
    created_at: str
    schema_version: int = SCHEMA_VERSION
    integrity_sha256: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "keep": self.keep,
            "reasons": list(self.reasons),
            "improvements": list(self.improvements),
            "regressions": list(self.regressions),
            "blocking_regressions": list(self.blocking_regressions),
            "baseline_report_sha256": self.baseline_report_sha256,
            "candidate_report_sha256": self.candidate_report_sha256,
            "spec_sha256": self.spec_sha256,
            "created_at": self.created_at,
        }

    def with_integrity(self) -> "KeepDecision":
        digest = sha256_json(self.unsigned_dict())
        return replace(self, integrity_sha256=digest)

    def verify_integrity(self) -> bool:
        return bool(self.integrity_sha256) and self.integrity_sha256 == sha256_json(
            self.unsigned_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["integrity_sha256"] = self.integrity_sha256
        return payload
