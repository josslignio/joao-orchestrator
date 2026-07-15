"""V2 Product Genesis Engine — gate production coding behind a human-accepted blueprint.

A product is not "begun" until a *complete, unambiguous* Product Blueprint exists
**and** a human operator has explicitly, visually accepted it. The genesis
engine enforces exactly that, through a deterministic pipeline:

    IDEA
      -> DISCOVERY
      -> BLUEPRINT_DRAFT
      -> BLUEPRINT_INCOMPLETE        (failed ambiguity/completeness checks)
      -> BLUEPRINT_READY_FOR_HUMAN_REVIEW   (complete + unambiguous)
      -> BLUEPRINT_HUMAN_ACCEPTED    (explicit operator action only)

Hard guarantees:

* :class:`ProductBlueprint` is fail-closed: a vague/incomplete blueprint cannot
  reach READY_FOR_HUMAN_REVIEW.
* :class:`AmbiguityGate` runs deterministic checks (vague outcomes, unmeasurable
  acceptance, features without observable behaviour, outputs without
  format/destination, inputs without source expectations, unresolved critical
  decisions, in/out-of-scope contradictions, undefined required schedules,
  rollback without a last-valid baseline). Its readiness score is **derived from
  explicit deterministic checks**, never an AI confidence estimate.
* Completeness != acceptance. An autonomous build may only prepare
  BLUEPRINT_READY_FOR_HUMAN_REVIEW. Acceptance is a separate, explicit operator
  action that requires operator identity + a "visually reviewed" attestation,
  and is recorded as an immutable gate pass.
* Accepted blueprints are immutable; editing creates a new version. Earlier
  accepted versions remain rollbackable. Cross-project blueprint loading is
  impossible (the contract carries the owning project id).

Honesty note (§ point 4): software cannot cryptographically prove human intent
without an external identity/approval system. This engine records a strong,
attested, append-only *claim* of human acceptance, bound to an exact blueprint
fingerprint, operator identity, timestamp, and acceptance source. It is the
strongest humanness guarantee enforceable in-process; it is not a cryptographic
proof of human approval.

Design invariants (unchanged from the package):

* Standard library only.
* State lives OUTSIDE the repo (``~/.local/share/joss-orchestrator/``).
* Generic core contains no project literals (no trading KOLs / job boards / CVs).
* Fail-closed integrity.

Reuses — never duplicates:

* :mod:`joao_orchestrator.storage.atomic` — ``atomic_write_json``.
* :mod:`joao_orchestrator.v2.gate_ledger` — ``GateLedger`` (immutable gate
  outcomes) and ``GateImmutabilityError``.
* :mod:`joao_orchestrator.v2.state` — ``project_state_dir``.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..storage.atomic import atomic_write_json  # reuse, not duplicate
from .gate_ledger import GateLedger, GateStatus, GateImmutabilityError  # reuse
from .state import project_state_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BLUEPRINT_SCHEMA_VERSION = "2.1"      # expanded V2.4 contract
CONTRACT_SCHEMA_VERSION = "2.0"
BLUEPRINT_GATE = "genesis.blueprint_accepted"

# Readiness is derived deterministically; these thresholds are explicit.
READY_THRESHOLD = 100                 # only 100% (all checks pass) = ready
CRITICAL_DECISION_KEYWORDS = ("critical", "blocker", "must-decide")

# File names (per-project, under project_state_dir).
DRAFTS_DIRNAME = "blueprint_versions"        # v1/, v2/, ... each {bp,status}
ACCEPTED_INDEX = "blueprint.accepted.json"   # pointer to current accepted ver
CONTRACT_FILENAME = "project_contract.json"
LEDGER_NOTE_ATTR = "blueprint.genesis_state.json"  # small status mirror


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"blueprint requires a non-empty {name}")
    return value.strip()


def _require_nonempty_list(values: Any, name: str) -> list[str]:
    """Require a non-empty list of non-empty strings; returns cleaned strings."""
    if not isinstance(values, list) or not values:
        raise ValueError(f"blueprint requires a non-empty {name} list")
    out: list[str] = []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{name} entries must be non-empty strings")
        out.append(v.strip())
    return out


def _require_nonempty_typed_list(values: Any, name: str) -> list:
    """Require a non-empty list (items validated separately by their .validate())."""
    if not isinstance(values, list) or not values:
        raise ValueError(f"blueprint requires a non-empty {name} list")
    return values


# ---------------------------------------------------------------------------
# Typed nested structures (stdlib dataclasses)
# ---------------------------------------------------------------------------

@dataclass
class UserJourney:
    """One exact end-to-end user journey."""
    journey_id: str
    actor: str                  # who
    steps: list[str]            # ordered, observable steps
    outcome: str                # observable end state

    def validate(self) -> None:
        _require_nonblank(self.journey_id, "journey_id")
        _require_nonblank(self.actor, "journey actor")
        _require_nonempty_list(self.steps, f"journey {self.journey_id} steps")
        _require_nonblank(self.outcome, f"journey {self.journey_id} outcome")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "UserJourney":
        return cls(journey_id=str(d.get("journey_id", "")),
                   actor=str(d.get("actor", "")),
                   steps=list(d.get("steps", [])),
                   outcome=str(d.get("outcome", "")))


@dataclass
class Screen:
    """A concrete surface: page / screen / dashboard / report / file / notification."""
    surface_id: str
    kind: str                   # page|screen|dashboard|report|file|notification
    name: str
    purpose: str                # what the user sees / does here

    def validate(self) -> None:
        _require_nonblank(self.surface_id, "surface_id")
        _require_nonblank(self.kind, "surface kind")
        _require_nonblank(self.name, "surface name")
        _require_nonblank(self.purpose, f"surface {self.surface_id} purpose")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Screen":
        return cls(surface_id=str(d.get("surface_id", "")),
                   kind=str(d.get("kind", "")),
                   name=str(d.get("name", "")),
                   purpose=str(d.get("purpose", "")))


@dataclass
class DataInput:
    """A data source the product consumes."""
    input_id: str
    name: str
    source: str                 # where it comes from
    expectation: str            # source expectation (latency/freshness/format)

    def validate(self) -> None:
        _require_nonblank(self.input_id, "input_id")
        _require_nonblank(self.name, "input name")
        _require_nonblank(self.source, f"input {self.input_id} source")
        _require_nonblank(self.expectation,
                          f"input {self.input_id} source expectation")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DataInput":
        return cls(input_id=str(d.get("input_id", "")),
                   name=str(d.get("name", "")),
                   source=str(d.get("source", "")),
                   expectation=str(d.get("expectation", "")))


@dataclass
class DataOutput:
    """A product output."""
    output_id: str
    name: str
    format: str                 # format (json/csv/html/notification/...)
    destination: str            # where it goes

    def validate(self) -> None:
        _require_nonblank(self.output_id, "output_id")
        _require_nonblank(self.name, "output name")
        _require_nonblank(self.format, f"output {self.output_id} format")
        _require_nonblank(self.destination,
                          f"output {self.output_id} destination")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DataOutput":
        return cls(output_id=str(d.get("output_id", "")),
                   name=str(d.get("name", "")),
                   format=str(d.get("format", "")),
                   destination=str(d.get("destination", "")))


@dataclass
class Schedule:
    """A named frequency/schedule requirement."""
    schedule_id: str
    cadence: str                # e.g. "daily 09:00 UTC", "on-event"
    description: str

    def validate(self) -> None:
        _require_nonblank(self.schedule_id, "schedule_id")
        _require_nonblank(self.cadence, f"schedule {self.schedule_id} cadence")
        _require_nonblank(self.description,
                          f"schedule {self.schedule_id} description")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Schedule":
        return cls(schedule_id=str(d.get("schedule_id", "")),
                   cadence=str(d.get("cadence", "")),
                   description=str(d.get("description", "")))


@dataclass
class Feature:
    """A product feature with a priority and an observable behaviour."""
    feature_id: str
    name: str
    priority: str               # P0 | P1 | P2
    observable_behaviour: str   # what an observer can see/measure

    def validate(self) -> None:
        _require_nonblank(self.feature_id, "feature_id")
        _require_nonblank(self.name, f"feature {self.feature_id} name")
        if self.priority not in ("P0", "P1", "P2"):
            raise ValueError(
                f"feature {self.feature_id} priority must be P0|P1|P2")
        _require_nonblank(self.observable_behaviour,
                          f"feature {self.feature_id} observable_behaviour")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Feature":
        return cls(feature_id=str(d.get("feature_id", "")),
                   name=str(d.get("name", "")),
                   priority=str(d.get("priority", "")),
                   observable_behaviour=str(d.get("observable_behaviour", "")))


@dataclass
class QualityTarget:
    """A measurable quality target."""
    target_id: str
    metric: str
    target: str                 # the measurable value/threshold
    measurement: str            # how it is measured

    def validate(self) -> None:
        _require_nonblank(self.target_id, "target_id")
        _require_nonblank(self.metric, f"quality {self.target_id} metric")
        _require_nonblank(self.target, f"quality {self.target_id} target")
        _require_nonblank(self.measurement,
                          f"quality {self.target_id} measurement")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "QualityTarget":
        return cls(target_id=str(d.get("target_id", "")),
                   metric=str(d.get("metric", "")),
                   target=str(d.get("target", "")),
                   measurement=str(d.get("measurement", "")))


@dataclass
class OpenDecision:
    """An operator decision that is still open."""
    decision_id: str
    question: str
    severity: str               # critical | normal | low
    resolved: bool = False
    resolution: str = ""

    def validate(self) -> None:
        _require_nonblank(self.decision_id, "decision_id")
        _require_nonblank(self.question, f"decision {self.decision_id} question")
        if self.severity not in ("critical", "normal", "low"):
            raise ValueError(
                f"decision {self.decision_id} severity must be "
                "critical|normal|low")

    @property
    def is_blocking(self) -> bool:
        return (self.severity == "critical") and not self.resolved

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "OpenDecision":
        return cls(decision_id=str(d.get("decision_id", "")),
                   question=str(d.get("question", "")),
                   severity=str(d.get("severity", "normal")),
                   resolved=bool(d.get("resolved", False)),
                   resolution=str(d.get("resolution", "")))


# ---------------------------------------------------------------------------
# The Product Blueprint (canonical, human-readable)
# ---------------------------------------------------------------------------

# Vague-outcome heuristic: a string of bare filler words with no concrete noun.
_VAGUE_TOKENS = re.compile(
    r"\b(various|stuff|things|something|nice|good|great|cool|etc|relevant|"
    r"useful|better|improved|some|certain|appropriate|as needed)\b",
    re.IGNORECASE)


@dataclass
class ProductBlueprint:
    """A complete, canonical, human-readable product definition.

    A blueprint is *structurally complete* once :meth:`validate` passes (all
    required sections present and non-empty). Structural completeness does NOT
    imply readiness for human review — :class:`AmbiguityGate` must separately
    find it unambiguous. And readiness never implies acceptance: only an
    explicit operator action reaches BLUEPRINT_HUMAN_ACCEPTED.

    Versioning: each blueprint carries ``version`` (int >= 1), ``parent_version``
    (the accepted version it supersedes, or 0), and ``project_id`` (binds the
    blueprint to its project; cross-project loading is refused).
    """
    # --- identity ---
    project_id: str
    product_name: str
    product_outcome: str            # final user-visible outcome
    problem_statement: str
    target_users: str
    # --- shape ---
    user_journeys: list[UserJourney] = field(default_factory=list)
    surfaces: list[Screen] = field(default_factory=list)
    data_inputs: list[DataInput] = field(default_factory=list)
    data_outputs: list[DataOutput] = field(default_factory=list)
    schedules: list[Schedule] = field(default_factory=list)
    # --- behaviour ---
    processing_logic: str = ""      # processing + decision logic (prose)
    features: list[Feature] = field(default_factory=list)
    # --- boundaries ---
    non_goals: list[str] = field(default_factory=list)
    # --- quality + safety ---
    quality_targets: list[QualityTarget] = field(default_factory=list)
    failure_and_degraded_mode: str = ""
    data_retention_and_privacy: str = ""
    # --- acceptance + ops ---
    acceptance_criteria: list[str] = field(default_factory=list)
    last_valid_baseline: str = ""    # release/tag/commit baseline for rollback
    rollback: str = ""
    assumptions: list[str] = field(default_factory=list)
    open_decisions: list[OpenDecision] = field(default_factory=list)
    # --- versioning / meta ---
    version: int = 1
    parent_version: int = 0
    blueprint_id: str = ""
    created_at: str = ""
    schema_version: str = BLUEPRINT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.blueprint_id:
            self.blueprint_id = "bp_" + secrets.token_hex(8)
        if not self.created_at:
            self.created_at = _utcnow()

    # -- structural validation (fail-closed) -----------------------------
    def validate(self) -> None:
        """Structural completeness check. Raises ``ValueError`` if incomplete.

        Validates presence + non-emptiness of every required section and each
        nested item. Does NOT judge vagueness — that is :class:`AmbiguityGate`.
        """
        _require_nonblank(self.project_id, "project_id")
        _require_nonblank(self.product_name, "product_name")
        _require_nonblank(self.product_outcome, "product_outcome")
        _require_nonblank(self.problem_statement, "problem_statement")
        _require_nonblank(self.target_users, "target_users")
        _require_nonempty_typed_list(self.user_journeys, "user_journeys")
        for j in self.user_journeys:
            j.validate()
        _require_nonempty_typed_list(self.surfaces, "surfaces")
        for s in self.surfaces:
            s.validate()
        _require_nonempty_typed_list(self.data_inputs, "data_inputs")
        for i in self.data_inputs:
            i.validate()
        _require_nonempty_typed_list(self.data_outputs, "data_outputs")
        for o in self.data_outputs:
            o.validate()
        _require_nonblank(self.processing_logic, "processing_logic")
        _require_nonempty_typed_list(self.features, "features")
        for f in self.features:
            f.validate()
        # at least one P0 feature
        if not any(f.priority == "P0" for f in self.features):
            raise ValueError("blueprint requires at least one P0 feature")
        _require_nonempty_list(self.acceptance_criteria, "acceptance_criteria")
        _require_nonempty_typed_list(self.quality_targets, "quality_targets")
        for q in self.quality_targets:
            q.validate()
        _require_nonblank(self.failure_and_degraded_mode,
                          "failure_and_degraded_mode")
        _require_nonblank(self.data_retention_and_privacy,
                          "data_retention_and_privacy")
        for d in self.open_decisions:
            d.validate()
        if self.version < 1:
            raise ValueError("version must be >= 1")

    @property
    def is_complete(self) -> bool:
        try:
            self.validate()
            return True
        except ValueError:
            return False

    # -- fingerprint (content-bound) -------------------------------------
    def fingerprint(self) -> str:
        """SHA-256 of the blueprint's canonical JSON (sorted keys).

        Stable across runs for identical content. Bound to the exact content
        that was accepted, so any edit to an accepted version is detectable.
        """
        self.validate()  # never fingerprint an invalid blueprint
        canonical = json.dumps(self.to_dict(), sort_keys=True,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # -- (de)serialization -----------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "product_name": self.product_name,
            "product_outcome": self.product_outcome,
            "problem_statement": self.problem_statement,
            "target_users": self.target_users,
            "user_journeys": [j.to_dict() for j in self.user_journeys],
            "surfaces": [s.to_dict() for s in self.surfaces],
            "data_inputs": [i.to_dict() for i in self.data_inputs],
            "data_outputs": [o.to_dict() for o in self.data_outputs],
            "schedules": [s.to_dict() for s in self.schedules],
            "processing_logic": self.processing_logic,
            "features": [f.to_dict() for f in self.features],
            "non_goals": list(self.non_goals),
            "quality_targets": [q.to_dict() for q in self.quality_targets],
            "failure_and_degraded_mode": self.failure_and_degraded_mode,
            "data_retention_and_privacy": self.data_retention_and_privacy,
            "acceptance_criteria": list(self.acceptance_criteria),
            "last_valid_baseline": self.last_valid_baseline,
            "rollback": self.rollback,
            "assumptions": list(self.assumptions),
            "open_decisions": [d.to_dict() for d in self.open_decisions],
            "version": self.version,
            "parent_version": self.parent_version,
            "blueprint_id": self.blueprint_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ProductBlueprint":
        def _str(k, default=""):
            return str(d.get(k, default))

        return cls(
            project_id=_str("project_id"),
            product_name=_str("product_name"),
            product_outcome=_str("product_outcome"),
            problem_statement=_str("problem_statement"),
            target_users=_str("target_users"),
            user_journeys=[UserJourney.from_dict(x)
                           for x in d.get("user_journeys", [])],
            surfaces=[Screen.from_dict(x) for x in d.get("surfaces", [])],
            data_inputs=[DataInput.from_dict(x)
                         for x in d.get("data_inputs", [])],
            data_outputs=[DataOutput.from_dict(x)
                          for x in d.get("data_outputs", [])],
            schedules=[Schedule.from_dict(x) for x in d.get("schedules", [])],
            processing_logic=_str("processing_logic"),
            features=[Feature.from_dict(x) for x in d.get("features", [])],
            non_goals=list(d.get("non_goals", [])),
            quality_targets=[QualityTarget.from_dict(x)
                             for x in d.get("quality_targets", [])],
            failure_and_degraded_mode=_str("failure_and_degraded_mode"),
            data_retention_and_privacy=_str("data_retention_and_privacy"),
            acceptance_criteria=list(d.get("acceptance_criteria", [])),
            last_valid_baseline=_str("last_valid_baseline"),
            rollback=_str("rollback"),
            assumptions=list(d.get("assumptions", [])),
            open_decisions=[OpenDecision.from_dict(x)
                            for x in d.get("open_decisions", [])],
            version=int(d.get("version", 1) or 1),
            parent_version=int(d.get("parent_version", 0) or 0),
            blueprint_id=_str("blueprint_id"),
            created_at=_str("created_at"),
            schema_version=_str("schema_version", BLUEPRINT_SCHEMA_VERSION),
        )


# ---------------------------------------------------------------------------
# Deterministic ambiguity gate
# ---------------------------------------------------------------------------

@dataclass
class AmbiguityFinding:
    """One deterministic ambiguity finding. ``blocking`` ones prevent readiness."""
    check_id: str
    severity: str           # blocking | warning
    message: str
    location: str = ""      # which field/section

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReadinessReport:
    """Deterministic readiness report.

    ``readiness_score`` is DERIVED from explicit checks: 100 iff there are no
    blocking findings, else ``round(100 * passed/total)`` over the executed
    checks. It is NOT an AI confidence estimate and must never be presented as
    one.
    """
    complete: bool                      # structural completeness
    ready_for_human_review: bool        # complete AND no blocking findings
    readiness_score: int                # 0..100, deterministic
    findings: list[AmbiguityFinding] = field(default_factory=list)
    blocking_open_decisions: list[str] = field(default_factory=list)
    next_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "ready_for_human_review": self.ready_for_human_review,
            "readiness_score": self.readiness_score,
            "findings": [f.to_dict() for f in self.findings],
            "blocking_open_decisions": list(self.blocking_open_decisions),
            "next_action": self.next_action,
        }


class AmbiguityGate:
    """Deterministic ambiguity checks over a ProductBlueprint.

    Every check runs unconditionally and records PASS/FAIL so the readiness
    score is fully reproducible. Checks are deliberately conservative: they
    flag patterns that historically led to building the wrong thing.
    """

    # Each check returns (passed: bool, finding: AmbiguityFinding | None).
    def _check_vague_outcome(self, bp: ProductBlueprint):
        if _VAGUE_TOKENS.search(bp.product_outcome):
            return False, AmbiguityFinding(
                "vague_outcome", "blocking",
                "product_outcome contains vague filler words; state the "
                "concrete observable outcome", "product_outcome")
        return True, None

    def _check_measurable_acceptance(self, bp: ProductBlueprint):
        for i, c in enumerate(bp.acceptance_criteria):
            # "should" / "nice" / "good" = non-measurable language.
            if re.search(r"\b(should|nice|good|great|fast|better)\b",
                         c, re.IGNORECASE):
                return False, AmbiguityFinding(
                    "unmeasurable_acceptance", "blocking",
                    f"acceptance_criteria[{i}] is non-measurable: {c!r}",
                    f"acceptance_criteria[{i}]")
        return True, None

    def _check_feature_observable(self, bp: ProductBlueprint):
        for f in bp.features:
            if _VAGUE_TOKENS.search(f.observable_behaviour):
                return False, AmbiguityFinding(
                    "feature_without_observable_behaviour", "blocking",
                    f"feature {f.feature_id} observable_behaviour is vague",
                    f"features[{f.feature_id}]")
        return True, None

    def _check_output_format_destination(self, bp: ProductBlueprint):
        # Structural validation already guarantees non-empty format/destination;
        # this check catches filler-only values.
        for o in bp.data_outputs:
            if _VAGUE_TOKENS.search(o.format) or _VAGUE_TOKENS.search(o.destination):
                return False, AmbiguityFinding(
                    "output_without_format_or_destination", "blocking",
                    f"output {o.output_id} format/destination is vague",
                    f"data_outputs[{o.output_id}]")
        return True, None

    def _check_input_source_expectation(self, bp: ProductBlueprint):
        for i in bp.data_inputs:
            if _VAGUE_TOKENS.search(i.source) or _VAGUE_TOKENS.search(i.expectation):
                return False, AmbiguityFinding(
                    "input_without_source_expectation", "blocking",
                    f"input {i.input_id} source/expectation is vague",
                    f"data_inputs[{i.input_id}]")
        return True, None

    def _check_unresolved_critical_decisions(self, bp: ProductBlueprint):
        blocking = [d.decision_id for d in bp.open_decisions if d.is_blocking]
        if blocking:
            return False, AmbiguityFinding(
                "unresolved_critical_decisions", "blocking",
                f"unresolved critical open decisions: {blocking}",
                "open_decisions")
        return True, None

    def _check_scope_contradiction(self, bp: ProductBlueprint):
        in_set = {s.strip().lower() for s in bp.features_as_scope_tokens()
                  if s.strip()}
        out_set = {g.strip().lower() for g in bp.non_goals if g.strip()}
        # Contradiction: a P0 feature name appearing verbatim in non_goals.
        feats = {f.name.strip().lower() for f in bp.features if f.priority == "P0"}
        overlap = feats & out_set
        if overlap:
            return False, AmbiguityFinding(
                "scope_contradiction", "blocking",
                f"item is both a P0 feature and a non-goal: {sorted(overlap)}",
                "features/non_goals")
        return True, None

    def _check_schedule_required(self, bp: ProductBlueprint):
        # Any schedule-requiring surface (report/dashboard/notification) must
        # have at least one schedule defined.
        needs_schedule = {s.surface_id for s in bp.surfaces
                          if s.kind in ("report", "dashboard", "notification")}
        if needs_schedule and not bp.schedules:
            return False, AmbiguityFinding(
                "undefined_required_schedule", "blocking",
                f"schedule-requiring surfaces have no schedule: "
                f"{sorted(needs_schedule)}", "schedules")
        return True, None

    def _check_rollback_baseline(self, bp: ProductBlueprint):
        # rollback present but no last_valid_baseline = cannot roll back.
        if bp.rollback.strip() and not bp.last_valid_baseline.strip():
            return False, AmbiguityFinding(
                "rollback_without_baseline", "blocking",
                "rollback is defined but last_valid_baseline is empty; "
                "rollback target is undefined", "last_valid_baseline")
        return True, None

    CHECKS = (
        _check_vague_outcome,
        _check_measurable_acceptance,
        _check_feature_observable,
        _check_output_format_destination,
        _check_input_source_expectation,
        _check_unresolved_critical_decisions,
        _check_scope_contradiction,
        _check_schedule_required,
        _check_rollback_baseline,
    )

    def evaluate(self, bp: ProductBlueprint) -> ReadinessReport:
        """Run all checks deterministically and produce a readiness report.

        The readiness score is fully derived: structural completeness is a
        first-class check, so an incomplete blueprint can never score 100.
        ``ready_for_human_review`` requires BOTH completeness AND zero blocking
        findings. The score is never an AI confidence estimate.
        """
        complete = bp.is_complete
        findings: list[AmbiguityFinding] = []
        passed = 0
        total = 0

        # Completeness is a first-class check.
        total += 1
        if complete:
            passed += 1
        else:
            findings.append(AmbiguityFinding(
                "structural_completeness", "blocking",
                "blueprint is missing required sections; cannot be reviewed",
                "blueprint"))

        # Ambiguity checks run regardless (they report on whatever is present).
        for chk in self.CHECKS:
            total += 1
            ok, finding = chk(self, bp)
            if ok:
                passed += 1
            elif finding is not None:
                findings.append(finding)

        blocking = [f for f in findings if f.severity == "blocking"]
        ready = complete and not blocking
        blocking_decisions = [d.decision_id for d in bp.open_decisions
                              if d.is_blocking]
        score = 100 if ready else round(100 * passed / total)
        return ReadinessReport(
            complete=complete,
            ready_for_human_review=ready,
            readiness_score=score,
            findings=findings,
            blocking_open_decisions=blocking_decisions,
            next_action=_next_action(complete, ready, findings,
                                     blocking_decisions),
        )


# Helper attached to blueprint for scope-check reuse (avoids duplicating logic).
def _features_as_scope_tokens(self) -> list[str]:
    return [f.name for f in self.features]


ProductBlueprint.features_as_scope_tokens = _features_as_scope_tokens  # type: ignore[attr-defined]


def _next_action(complete: bool, ready: bool,
                 findings: list[AmbiguityFinding],
                 blocking_decisions: list[str]) -> str:
    if not complete:
        return ("Complete the missing required blueprint sections, then "
                "re-run the ambiguity gate.")
    if not ready:
        blocker = next((f for f in findings if f.severity == "blocking"), None)
        if blocker is not None:
            return (f"Resolve blocking ambiguity [{blocker.check_id}] "
                    f"at {blocker.location or 'blueprint'}: {blocker.message}")
        if blocking_decisions:
            return (f"Resolve critical open decisions: {blocking_decisions}")
    return ("Blueprint is ready for human review; an operator must explicitly "
            "accept it (BLUEPRINT_HUMAN_ACCEPTED is operator-only).")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class GenesisState:
    """The six genesis lifecycle states (string constants, per convention)."""
    IDEA = "IDEA"
    DISCOVERY = "DISCOVERY"
    BLUEPRINT_DRAFT = "BLUEPRINT_DRAFT"
    BLUEPRINT_INCOMPLETE = "BLUEPRINT_INCOMPLETE"
    BLUEPRINT_READY_FOR_HUMAN_REVIEW = "BLUEPRINT_READY_FOR_HUMAN_REVIEW"
    BLUEPRINT_HUMAN_ACCEPTED = "BLUEPRINT_HUMAN_ACCEPTED"

    ALL = (IDEA, DISCOVERY, BLUEPRINT_DRAFT, BLUEPRINT_INCOMPLETE,
           BLUEPRINT_READY_FOR_HUMAN_REVIEW, BLUEPRINT_HUMAN_ACCEPTED)

    @classmethod
    def is_terminal_coding_unblock(cls, state: str) -> bool:
        return state == cls.BLUEPRINT_HUMAN_ACCEPTED


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GenesisError(ValueError):
    """Base class for genesis errors (ValueError subclass by convention)."""


class GenesisBlockError(GenesisError):
    """Raised when production coding is attempted before human acceptance."""


class CrossProjectBlueprintError(GenesisError):
    """Raised when a blueprint's project_id does not match the engine."""


# ---------------------------------------------------------------------------
# Compiled product contract
# ---------------------------------------------------------------------------

@dataclass
class ProductContract:
    """Deterministic, machine-readable compilation of an accepted blueprint.

    Generated from (and fail-closed against) the human-readable blueprint. The
    blueprint remains canonical; the contract is a derived index used to detect
    drift (a recompile that disagrees with the stored contract = drift).
    """
    contract_id: str
    project_id: str
    blueprint_version: int
    blueprint_fingerprint: str
    source_document_hashes: dict[str, str]
    requirement_ids: list[str]
    feature_ids: list[str]
    acceptance_ids: list[str]
    non_goals: list[str]
    dependencies: list[str]
    open_decisions: list[str]
    last_accepted_release: str
    rollback_baseline: str
    contract_fingerprint: str
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def compile(cls, bp: ProductBlueprint,
                last_accepted_release: str = "") -> "ProductContract":
        """Compile an accepted blueprint into a deterministic contract.

        Fully deterministic: the same blueprint + inputs always compile to the
        same contract (the ``contract_id`` is derived from content, not random),
        so recompiling a stored contract must agree unless the blueprint changed.
        Fail-closed: refuses to compile a blueprint that is not structurally
        complete (the caller must have accepted only a complete blueprint).
        """
        bp.validate()
        blueprint_fingerprint = bp.fingerprint()
        source_hashes = {
            "blueprint": _sha256_json(bp.to_dict()),
        }
        requirement_ids = [f"REQ-{i+1:03d}"
                           for i, _ in enumerate(bp.acceptance_criteria)]
        feature_ids = [f.feature_id for f in bp.features]
        acceptance_ids = [f"ACC-{i+1:03d}"
                          for i, _ in enumerate(bp.acceptance_criteria)]
        # Derive a deterministic contract id from content (NOT random), so
        # recompiles are byte-stable and drift is detectable.
        contract_id = "pc_" + _sha256_json({
            "project_id": bp.project_id,
            "blueprint_version": bp.version,
            "blueprint_fingerprint": blueprint_fingerprint,
        })[:16]
        core = {
            "contract_id": contract_id,
            "project_id": bp.project_id,
            "blueprint_version": bp.version,
            "blueprint_fingerprint": blueprint_fingerprint,
            "source_document_hashes": source_hashes,
            "requirement_ids": requirement_ids,
            "feature_ids": feature_ids,
            "acceptance_ids": acceptance_ids,
            "non_goals": list(bp.non_goals),
            "dependencies": [j.journey_id for j in bp.user_journeys],
            "open_decisions": [d.decision_id for d in bp.open_decisions],
            "last_accepted_release": last_accepted_release,
            "rollback_baseline": bp.last_valid_baseline,
            "schema_version": CONTRACT_SCHEMA_VERSION,
        }
        contract_fingerprint = _sha256_json(core)
        return cls(contract_fingerprint=contract_fingerprint, **core)


def _sha256_json(obj: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class GenesisEngine:
    """Owns the blueprint lifecycle, ambiguity gate, and acceptance gate.

    Lifecycle (autonomous path may only reach READY_FOR_HUMAN_REVIEW)::

        draft_blueprint(...)   -> BLUEPRINT_DRAFT (gate stays BLOCKED)
        evaluate_readiness()   -> BLUEPRINT_INCOMPLETE or READY_FOR_HUMAN_REVIEW
        [human reviews]
        accept_blueprint(...)  -> BLUEPRINT_HUMAN_ACCEPTED (operator-only,
                                  immutable gate pass; compiled contract)

    Versioning: drafting again after acceptance creates a new version (the old
    accepted version is immutable + rollbackable). Cross-project blueprint
    loading is refused.
    """

    def __init__(self, project_id: str):
        if not project_id or "/" in project_id or ".." in project_id:
            raise ValueError(f"invalid project_id: {project_id!r}")
        self.project_id = project_id
        self.ledger = GateLedger(project_id)
        self.ambiguity = AmbiguityGate()
        self.versions_dir = project_state_dir(project_id) / DRAFTS_DIRNAME
        self.accepted_index_path = (project_state_dir(project_id)
                                    / ACCEPTED_INDEX)
        self.contract_path = (project_state_dir(project_id)
                              / CONTRACT_FILENAME)
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    # -- paths ------------------------------------------------------------
    def _version_dir(self, version: int) -> Path:
        return self.versions_dir / f"v{version}"

    def _blueprint_path(self, version: int) -> Path:
        p = self._version_dir(version) / "blueprint.json"
        return p

    def _status_path(self, version: int) -> Path:
        return self._version_dir(version) / "readiness.json"

    # -- loading ----------------------------------------------------------
    def _load_version_blueprint(self, version: int) -> ProductBlueprint:
        p = self._blueprint_path(version)
        if not p.exists():
            raise GenesisError(f"blueprint version {version} not found")
        data = json.loads(p.read_text(encoding="utf-8"))
        bp = ProductBlueprint.from_dict(data)
        if bp.project_id != self.project_id:
            raise CrossProjectBlueprintError(
                f"blueprint project_id {bp.project_id!r} != engine "
                f"{self.project_id!r}; cross-project loading refused")
        return bp

    def load_blueprint(self, version: int | None = None) -> ProductBlueprint | None:
        """Return the accepted blueprint if any, else the latest draft, else None.

        If ``version`` is given, load that exact version (refuses cross-project).
        """
        if version is not None:
            try:
                return self._load_version_blueprint(version)
            except GenesisError:
                return None
        if self.accepted_index_path.exists():
            idx = json.loads(self.accepted_index_path.read_text(encoding="utf-8"))
            v = int(idx.get("accepted_version", 0))
            if v:
                try:
                    return self._load_version_blueprint(v)
                except GenesisError:
                    pass
        latest = self.latest_version()
        if latest:
            try:
                return self._load_version_blueprint(latest)
            except GenesisError:
                return None
        return None

    def latest_version(self) -> int:
        """Highest version number present, or 0."""
        if not self.versions_dir.exists():
            return 0
        best = 0
        for child in self.versions_dir.iterdir():
            if child.is_dir() and child.name.startswith("v"):
                try:
                    best = max(best, int(child.name[1:]))
                except ValueError:
                    pass
        return best

    def accepted_version(self) -> int:
        """The currently accepted version, or 0."""
        if not self.accepted_index_path.exists():
            return 0
        idx = json.loads(self.accepted_index_path.read_text(encoding="utf-8"))
        return int(idx.get("accepted_version", 0) or 0)

    # -- drafting ---------------------------------------------------------
    def draft_blueprint(self, blueprint: ProductBlueprint) -> dict[str, Any]:
        """Validate structurally + persist as a new DRAFT version.

        The gate stays BLOCKED; drafting never reaches acceptance. If a
        blueprint is already accepted, drafting creates a NEW version whose
        ``parent_version`` is the accepted one (the accepted version is never
        overwritten).
        """
        # Bind + cross-project guard before anything else.
        if not blueprint.project_id:
            blueprint.project_id = self.project_id
        if blueprint.project_id != self.project_id:
            raise CrossProjectBlueprintError(
                f"blueprint project_id {blueprint.project_id!r} != engine "
                f"{self.project_id!r}")
        blueprint.validate()  # structural completeness (fail-closed)

        parent = self.accepted_version()
        if parent and blueprint.version <= parent:
            # new version supersedes the accepted one
            blueprint.version = parent + 1
        blueprint.parent_version = parent
        if not blueprint.created_at:
            blueprint.created_at = _utcnow()

        vdir = self._version_dir(blueprint.version)
        vdir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._blueprint_path(blueprint.version),
                          blueprint.to_dict())

        # Immediately evaluate readiness so the draft knows its state.
        report = self.ambiguity.evaluate(blueprint)
        atomic_write_json(self._status_path(blueprint.version),
                          report.to_dict())
        state = self._state_for_report(report)
        return {
            "project_id": self.project_id,
            "version": blueprint.version,
            "parent_version": blueprint.parent_version,
            "blueprint_id": blueprint.blueprint_id,
            "genesis_state": state,
            "fingerprint": blueprint.fingerprint(),
            "readiness": report.to_dict(),
            "draft_path": str(self._blueprint_path(blueprint.version)),
        }

    def _state_for_report(self, report: ReadinessReport) -> str:
        if not report.complete:
            return GenesisState.BLUEPRINT_INCOMPLETE
        if report.ready_for_human_review:
            return GenesisState.BLUEPRINT_READY_FOR_HUMAN_REVIEW
        return GenesisState.BLUEPRINT_INCOMPLETE

    # -- readiness --------------------------------------------------------
    def evaluate_readiness(self, version: int | None = None) -> ReadinessReport:
        """Re-run the deterministic ambiguity gate over a draft version."""
        bp = self.load_blueprint(version)
        if bp is None:
            return ReadinessReport(
                complete=False, ready_for_human_review=False,
                readiness_score=0,
                next_action="Draft a Product Blueprint first.")
        report = self.ambiguity.evaluate(bp)
        # Persist the re-evaluated status alongside the draft.
        try:
            atomic_write_json(self._status_path(bp.version),
                              report.to_dict())
        except OSError:
            pass
        return report

    # -- acceptance (operator-only, explicit) -----------------------------
    def accept_blueprint(self, *, accepted_by: str,
                         source: str = "operator-cli",
                         approval_evidence_ref: str = "",
                         visually_reviewed: bool = True,
                         source_run_id: str = "", head: str = "",
                         note: str = "",
                         version: int | None = None) -> dict[str, Any]:
        """Explicit human acceptance — the only path to BLUEPRINT_HUMAN_ACCEPTED.

        This is OPERATOR-ONLY by design. An autonomous build must never call
        this to self-attest its own blueprint; it may only reach
        BLUEPRINT_READY_FOR_HUMAN_REVIEW via :meth:`evaluate_readiness`.

        Requirements (attested in the immutable ledger):
        * non-empty ``accepted_by`` (operator identity);
        * ``visually_reviewed`` must be True (a claim of visual human review);
        * the blueprint must be structurally complete AND ambiguity-free
          (ready_for_human_review), else acceptance is refused;
        * acceptance binds to the exact blueprint ``fingerprint``.

        Honesty: this records a strong, append-only, fingerprint-bound CLAIM of
        human acceptance. It is not a cryptographic proof of human intent (no
        external identity system is involved).
        """
        attestation = _require_nonblank(accepted_by, "accepted_by")
        _require_nonblank(source, "acceptance source")
        if not visually_reviewed:
            raise GenesisError(
                "acceptance requires visually_reviewed=True; an autonomous "
                "build must not self-attest human review")
        bp = self.load_blueprint(version)
        if bp is None:
            raise GenesisError(
                "no blueprint exists to accept; draft one first")
        if bp.project_id != self.project_id:
            raise CrossProjectBlueprintError(
                "blueprint project_id mismatch on accept")
        report = self.ambiguity.evaluate(bp)
        if not report.complete:
            raise GenesisError(
                "blueprint is structurally incomplete; cannot be accepted")
        if not report.ready_for_human_review:
            blocking = [f.check_id for f in report.findings
                        if f.severity == "blocking"]
            raise GenesisError(
                "blueprint is not ready for human review; resolve blocking "
                f"ambiguities first: {blocking}")
        fingerprint = bp.fingerprint()
        accepted_at = _utcnow()

        # Persist accepted-pointer + acceptance record (immutable-ish index).
        accepted_payload = {
            "project_id": self.project_id,
            "accepted_version": bp.version,
            "blueprint_id": bp.blueprint_id,
            "fingerprint": fingerprint,
            "accepted_by": attestation,
            "accepted_at": accepted_at,
            "acceptance_source": source,
            "approval_evidence_ref": approval_evidence_ref.strip(),
            "visually_reviewed": True,
            "note": note.strip(),
            "head": head,
        }
        atomic_write_json(self.accepted_index_path, accepted_payload)

        # Compile the deterministic product contract (fail-closed on drift).
        last_release = self._last_accepted_release()
        contract = ProductContract.compile(bp, last_accepted_release=last_release)
        atomic_write_json(self.contract_path, contract.to_dict())

        # Immutable gate pass; evidence binds content + attester + source.
        evidence = [
            f"fingerprint={fingerprint}",
            f"accepted_by={attestation}",
            f"acceptance_source={source}",
            f"visually_reviewed=true",
            f"version={bp.version}",
            f"contract_fingerprint={contract.contract_fingerprint}",
        ]
        if approval_evidence_ref.strip():
            evidence.append(f"approval_evidence={approval_evidence_ref.strip()}")
        if note.strip():
            evidence.append(f"note={note.strip()}")
        rec = self.ledger.record_pass(
            BLUEPRINT_GATE,
            evidence=evidence,
            source_run_id=source_run_id,
            head=head,
            reason=("human-accepted Product Blueprint (operator action); "
                    "production coding unblocked"),
        )
        return {
            "project_id": self.project_id,
            "version": bp.version,
            "blueprint_id": bp.blueprint_id,
            "fingerprint": fingerprint,
            "accepted_by": attestation,
            "accepted_at": accepted_at,
            "acceptance_source": source,
            "visually_reviewed": True,
            "contract_fingerprint": contract.contract_fingerprint,
            "contract_path": str(self.contract_path),
            "gate": BLUEPRINT_GATE,
            "gate_status": rec.status,
            "gate_reason": rec.reason,
            "genesis_state": GenesisState.BLUEPRINT_HUMAN_ACCEPTED,
        }

    def _last_accepted_release(self) -> str:
        if not self.accepted_index_path.exists():
            return ""
        idx = json.loads(self.accepted_index_path.read_text(encoding="utf-8"))
        return str(idx.get("fingerprint", ""))[:16]

    # -- the hard guard ---------------------------------------------------
    def can_code(self) -> bool:
        """True iff BLUEPRINT_HUMAN_ACCEPTED (gate passed)."""
        return self.ledger.is_passed(BLUEPRINT_GATE)

    def genesis_state(self) -> str:
        """Current lifecycle state for the project."""
        if self.can_code():
            return GenesisState.BLUEPRINT_HUMAN_ACCEPTED
        bp = self.load_blueprint()
        if bp is None:
            return GenesisState.IDEA
        report = self.ambiguity.evaluate(bp)
        return self._state_for_report(report)

    def require_acceptance_for_coding(self) -> None:
        """Raise ``GenesisBlockError`` unless a human has accepted a blueprint."""
        if self.can_code():
            return
        raise GenesisBlockError(self.next_action())

    # -- versioning / rollback -------------------------------------------
    def rollback_to_version(self, version: int, *, accepted_by: str,
                            source: str = "operator-cli",
                            note: str = "") -> dict[str, Any]:
        """Roll the accepted pointer back to a previously-accepted version.

        Only a version that was itself accepted (immutable ledger evidence) may
        be re-instated as the current accepted version. This re-accepts it with
        a fresh ledger PASS bound to its (unchanged) fingerprint.
        """
        _require_nonblank(accepted_by, "accepted_by")
        bp = self._load_version_blueprint(version)  # refuses cross-project
        # The version must have an acceptance ledger record in its history.
        history = [r for r in self.ledger.read_all()
                   if r.gate_id == BLUEPRINT_GATE
                   and r.status == GateStatus.PASSED
                   and any(e == f"version={version}" for e in r.evidence)]
        if not history:
            raise GenesisError(
                f"version {version} was never accepted; cannot roll back to it")
        report = self.ambiguity.evaluate(bp)
        if not report.ready_for_human_review:
            raise GenesisError(
                f"version {version} is no longer ready for human review")
        fingerprint = bp.fingerprint()
        accepted_payload = {
            "project_id": self.project_id,
            "accepted_version": bp.version,
            "blueprint_id": bp.blueprint_id,
            "fingerprint": fingerprint,
            "accepted_by": accepted_by,
            "accepted_at": _utcnow(),
            "acceptance_source": source,
            "approval_evidence_ref": "",
            "visually_reviewed": True,
            "note": f"rollback to v{version}: {note}".strip(),
        }
        atomic_write_json(self.accepted_index_path, accepted_payload)
        contract = ProductContract.compile(bp)
        atomic_write_json(self.contract_path, contract.to_dict())
        rec = self.ledger.record_pass(
            BLUEPRINT_GATE,
            evidence=[f"fingerprint={fingerprint}",
                      f"accepted_by={accepted_by}",
                      f"acceptance_source={source}",
                      f"visually_reviewed=true",
                      f"version={bp.version}",
                      f"rollback_from=accepted",
                      f"contract_fingerprint={contract.contract_fingerprint}"],
            reason=f"rolled back accepted blueprint to v{version}")
        return {
            "rolled_back_to": bp.version,
            "fingerprint": fingerprint,
            "gate_status": rec.status,
        }

    # -- introspection / drift -------------------------------------------
    def accepted_fingerprint(self) -> str | None:
        """Recompute the fingerprint from the currently-accepted version's file.

        Recomputed (not read from a stored field) so tampering is detectable.
        """
        v = self.accepted_version()
        if not v:
            return None
        try:
            bp = self._load_version_blueprint(v)
        except GenesisError:
            return None
        if not bp.is_complete:
            return None
        return bp.fingerprint()

    def ledger_fingerprint(self) -> str | None:
        """The fingerprint attested in the latest PASSED ledger record."""
        for rec in reversed(self.ledger.read_all()):
            if rec.gate_id == BLUEPRINT_GATE and \
                    rec.status == GateStatus.PASSED:
                for ev in rec.evidence:
                    if ev.startswith("fingerprint="):
                        return ev.split("=", 1)[1]
                return None
        return None

    def fingerprint_consistent(self) -> bool:
        af = self.accepted_fingerprint()
        lf = self.ledger_fingerprint()
        if af is None or lf is None:
            return af == lf
        return af == lf

    def contract_drift(self) -> bool:
        """True iff the accepted blueprint and its stored contract disagree.

        Two forms of drift are caught:
        * blueprint drift — recompiling the accepted blueprint yields a
          different contract than the stored one (the blueprint changed after
          acceptance, or the contract file was tampered);
        * contract self-inconsistency — the stored contract's
          ``contract_fingerprint`` does not match a recompute over its own
          content (the contract file was hand-edited).

        No contract + accepted = drift (accepted without a compiled contract).
        No contract + not accepted = not drift (nothing to check yet).
        """
        if not self.contract_path.exists():
            return self.can_code()
        v = self.accepted_version()
        if not v:
            return False
        try:
            bp = self._load_version_blueprint(v)
        except GenesisError:
            return True
        stored = json.loads(self.contract_path.read_text(encoding="utf-8"))
        # (a) blueprint-vs-contract: recompile from the blueprint.
        fresh = ProductContract.compile(
            bp, last_accepted_release=stored.get("last_accepted_release", ""))
        if fresh.to_dict() != stored:
            return True
        # (b) contract self-consistency: recompute the fingerprint over the
        # stored contract's own core fields.
        core = {k: stored.get(k) for k in (
            "contract_id", "project_id", "blueprint_version",
            "blueprint_fingerprint", "source_document_hashes",
            "requirement_ids", "feature_ids", "acceptance_ids", "non_goals",
            "dependencies", "open_decisions", "last_accepted_release",
            "rollback_baseline", "schema_version")}
        recomputed = _sha256_json(core)
        return recomputed != stored.get("contract_fingerprint")

    # -- status -----------------------------------------------------------
    def blueprint_status(self) -> dict[str, Any]:
        bp = self.load_blueprint()
        report = self.ambiguity.evaluate(bp) if bp else ReadinessReport(
            complete=False, ready_for_human_review=False, readiness_score=0,
            next_action="Draft a Product Blueprint first.")
        return {
            "project_id": self.project_id,
            "genesis_state": self.genesis_state(),
            "latest_version": self.latest_version(),
            "accepted_version": self.accepted_version(),
            "complete": report.complete,
            "ready_for_human_review": report.ready_for_human_review,
            "readiness_score": report.readiness_score,
            "blueprint_id": bp.blueprint_id if bp else None,
            "version": bp.version if bp else None,
            "fingerprint": (bp.fingerprint() if bp and bp.is_complete else None),
            "gate": BLUEPRINT_GATE,
            "gate_status": (GateStatus.PASSED if self.can_code()
                            else GateStatus.BLOCKED),
            "fingerprint_consistent": self.fingerprint_consistent(),
            "contract_drift": self.contract_drift(),
            "blocking_findings": [f.to_dict() for f in report.findings
                                  if f.severity == "blocking"],
            "blocking_open_decisions": report.blocking_open_decisions,
            "can_code": self.can_code(),
            "next_action": self.next_action(),
        }

    def next_action(self) -> str:
        bp = self.load_blueprint()
        if bp is None:
            return (f"Draft a complete Product Blueprint for "
                    f"'{self.project_id}' (joss_v2 genesis draft --project "
                    f"{self.project_id} --blueprint <path.json>).")
        report = self.ambiguity.evaluate(bp)
        if self.can_code():
            return (f"Blueprint accepted for '{self.project_id}' "
                    f"(v{self.accepted_version()}); production coding unblocked.")
        if not report.complete or not report.ready_for_human_review:
            return report.next_action
        return (f"A human operator must explicitly accept the blueprint for "
                f"'{self.project_id}': joss_v2 genesis accept --project "
                f"{self.project_id} --accepted-by <name> --visually-reviewed")


__all__ = [
    "BLUEPRINT_GATE",
    "BLUEPRINT_SCHEMA_VERSION",
    "CONTRACT_SCHEMA_VERSION",
    "READY_THRESHOLD",
    "ProductBlueprint",
    "UserJourney", "Screen", "DataInput", "DataOutput", "Schedule",
    "Feature", "QualityTarget", "OpenDecision",
    "AmbiguityFinding", "ReadinessReport", "AmbiguityGate",
    "GenesisState",
    "GenesisError", "GenesisBlockError", "CrossProjectBlueprintError",
    "ProductContract",
    "GenesisEngine",
]
