"""V2 roadmap engine — ordered DAG of milestones and gates (§9).

A roadmap is an ordered DAG. Each milestone has a product outcome, an ordered
list of gates, dependencies on other milestones, and the delivery bottleneck
it removes. The engine:

* validates the DAG (acyclic, single active milestone, single active gate);
* returns the next eligible gate (first gate whose milestone deps are passed
  and whose own status is not yet PASSED);
* passes/blocks gates through the :class:`~joao_orchestrator.v2.gate_ledger.GateLedger`;
* renders a concise one-screen roadmap summary.

Architecture milestones must state which *delivery bottleneck* they remove —
internal architecture may not replace product outcomes (§9).

Persistence reuses the existing atomic store; the roadmap lives at
``projects/<id>/state/roadmap.json``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..storage.atomic import atomic_write_json  # reuse
from .gate_ledger import GateLedger, GateStatus
from .state import project_state_dir

ROADMAP_FILENAME = "roadmap.json"


def roadmap_path(project_id: str) -> Path:
    return project_state_dir(project_id) / ROADMAP_FILENAME


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

MILESTONE_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "COMPLETED", "BLOCKED")


@dataclass
class Gate:
    gate_id: str
    title: str = ""
    acceptance_command: str = ""   # deterministic command proving the gate
    # When True, passing this gate marks its milestone complete.

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Milestone:
    milestone_id: str
    title: str
    product_outcome: str                 # user-visible outcome, not architecture
    dependencies: list[str] = field(default_factory=list)
    gates: list[Gate] = field(default_factory=list)
    status: str = "NOT_STARTED"
    visible_artifact: str = ""
    acceptance_command: str = ""
    prohibited_scope: list[str] = field(default_factory=list)
    delivery_bottleneck_removed: str = ""  # required for architecture milestones

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Roadmap:
    project_id: str
    global_product_goal: str
    milestones: list[Milestone] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "project_id": self.project_id,
            "global_product_goal": self.global_product_goal,
            "milestones": [m.to_dict() for m in self.milestones],
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class RoadmapValidationError(ValueError):
    pass


def validate_roadmap(rm: Roadmap) -> None:
    """Validate the DAG structure and the §9 single-active invariants."""
    if not rm.milestones:
        raise RoadmapValidationError("roadmap must have at least one milestone")
    ids = [m.milestone_id for m in rm.milestones]
    if len(set(ids)) != len(ids):
        raise RoadmapValidationError(f"duplicate milestone ids: {ids}")
    idset = set(ids)
    # dependency closure must exist + be acyclic
    for m in rm.milestones:
        for dep in m.dependencies:
            if dep not in idset:
                raise RoadmapValidationError(
                    f"milestone {m.milestone_id!r} depends on unknown {dep!r}")
        if not m.product_outcome.strip():
            raise RoadmapValidationError(
                f"milestone {m.milestone_id!r} has no product_outcome")
        if not m.gates:
            raise RoadmapValidationError(
                f"milestone {m.milestone_id!r} has no gates")
        for g in m.gates:
            if not g.gate_id:
                raise RoadmapValidationError(
                    f"milestone {m.milestone_id!r} has a gate with no gate_id")
    _assert_acyclic(rm)


def _assert_acyclic(rm: Roadmap) -> None:
    """Topological check; raises on any cycle."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {m.milestone_id: WHITE for m in rm.milestones}
    edges = {m.milestone_id: list(m.dependencies) for m in rm.milestones}

    def visit(node: str, path: list[str]) -> None:
        if color[node] == GRAY:
            raise RoadmapValidationError(
                f"roadmap has a cycle through {node!r}: {' -> '.join(path + [node])}"
            )
        if color[node] == BLACK:
            return
        color[node] = GRAY
        for dep in edges[node]:
            visit(dep, path + [node])
        color[node] = BLACK

    for n in color:
        if color[n] == WHITE:
            visit(n, [])


# ---------------------------------------------------------------------------
# Engine: next gate, pass/block
# ---------------------------------------------------------------------------

class RoadmapEngine:
    """Reads roadmap + gate ledger, answers 'what is the next gate?'."""

    def __init__(self, project_id: str, rm: Roadmap):
        self.project_id = project_id
        self.roadmap = rm
        self.ledger = GateLedger(project_id)

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, project_id: str) -> "RoadmapEngine":
        p = roadmap_path(project_id)
        if not p.exists():
            raise FileNotFoundError(f"no roadmap.json at {p}")
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(project_id, _roadmap_from_dict(data))

    def save(self) -> None:
        validate_roadmap(self.roadmap)
        atomic_write_json(roadmap_path(self.project_id), self.roadmap.to_dict())

    # -- queries ----------------------------------------------------------
    def milestone_status(self, m: Milestone) -> str:
        deps_passed = all(
            self._milestone_passed(d) for d in m.dependencies)
        if not deps_passed:
            return "BLOCKED"
        gates_passed = [g.gate_id for g in m.gates
                        if self.ledger.is_passed(g.gate_id)]
        if len(gates_passed) == len(m.gates):
            return "COMPLETED"
        return "IN_PROGRESS" if gates_passed else "NOT_STARTED"

    def _milestone_passed(self, milestone_id: str) -> bool:
        m = next((x for x in self.roadmap.milestones
                  if x.milestone_id == milestone_id), None)
        if m is None:
            return False
        return self.milestone_status(m) == "COMPLETED"

    def active_milestone(self) -> Milestone | None:
        """The first milestone that is neither COMPLETED nor blocked by deps."""
        for m in self.roadmap.milestones:
            st = self.milestone_status(m)
            if st == "COMPLETED":
                continue
            if st == "BLOCKED":
                continue
            return m
        return None

    def next_gate(self) -> Gate | None:
        """The single next eligible gate (§9: one active gate).

        A gate is eligible when its milestone is the active one and the gate
        itself is not yet PASSED. Returns the first such gate in declaration
        order. Returns ``None`` if the roadmap is complete.
        """
        m = self.active_milestone()
        if m is None:
            return None
        for g in m.gates:
            if not self.ledger.is_passed(g.gate_id):
                return g
        return None

    def next_single_action(self) -> str:
        """The exact, single next action for project_state.next_single_action."""
        g = self.next_gate()
        if g is None:
            return "roadmap complete — define next milestone or stop"
        am = self.active_milestone()
        mtitle = am.title if am else ""
        if g.acceptance_command:
            return (
                f"PASS gate `{g.gate_id}` ({g.title}) in milestone "
                f"`{mtitle}` by running: {g.acceptance_command}"
            )
        return (
            f"PASS gate `{g.gate_id}` ({g.title}) in milestone `{mtitle}`"
        )

    # -- mutations --------------------------------------------------------
    def pass_gate(
        self, gate_id: str, *, evidence: Iterable[str] = (),
        source_run_id: str = "", head: str = "", reason: str = "",
    ) -> str:
        status = self.ledger.record_pass(
            gate_id, evidence=evidence, source_run_id=source_run_id,
            head=head, reason=reason).status
        return status

    def block_gate(self, gate_id: str, *, reason: str,
                   evidence: Iterable[str] = (), source_run_id: str = "",
                   head: str = "") -> str:
        """Block ``gate_id``.

        Raises :class:`~joao_orchestrator.v2.gate_ledger.GateImmutabilityError`
        if ``gate_id`` is currently PASSED — a passed gate cannot be reopened
        by blocking; it must be invalidated via a HIGH-confidence
        ``ContradictionRecord`` (§9/§34).
        """
        return self.ledger.record_block(
            gate_id, reason=reason, evidence=evidence,
            source_run_id=source_run_id, head=head).status

    # -- render -----------------------------------------------------------
    def render(self) -> str:
        """Concise one-screen roadmap render."""
        lines = [f"ROADMAP: {self.roadmap.global_product_goal}", ""]
        for m in self.roadmap.milestones:
            st = self.milestone_status(m)
            lines.append(f"[{st:11}] {m.milestone_id}: {m.title}")
            for g in m.gates:
                mark = "x" if self.ledger.is_passed(g.gate_id) else " "
                lines.append(f"             [{mark}] {g.gate_id} — {g.title}")
        g = self.next_gate()
        lines.append("")
        lines.append("NEXT: " + (g.gate_id if g else "(complete)"))
        return "\n".join(lines)


def _roadmap_from_dict(data: Mapping[str, Any]) -> Roadmap:
    milestones: list[Milestone] = []
    for md in data.get("milestones", []):
        gates = [Gate(**g) for g in md.get("gates", [])]
        milestones.append(Milestone(
            milestone_id=md["milestone_id"], title=md.get("title", ""),
            product_outcome=md.get("product_outcome", ""),
            dependencies=list(md.get("dependencies", [])), gates=gates,
            status=md.get("status", "NOT_STARTED"),
            visible_artifact=md.get("visible_artifact", ""),
            acceptance_command=md.get("acceptance_command", ""),
            prohibited_scope=list(md.get("prohibited_scope", [])),
            delivery_bottleneck_removed=md.get(
                "delivery_bottleneck_removed", ""),
        ))
    return Roadmap(
        project_id=data.get("project_id", ""),
        global_product_goal=data.get("global_product_goal", ""),
        milestones=milestones,
    )


__all__ = [
    "Gate", "Milestone", "Roadmap", "MILESTONE_STATUSES",
    "RoadmapValidationError", "validate_roadmap", "RoadmapEngine",
    "roadmap_path",
]
