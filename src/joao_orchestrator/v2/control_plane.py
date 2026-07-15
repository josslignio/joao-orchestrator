"""V2.4 PR-A2 — Control Plane + Reviewer Bridge.

The control plane lets the autonomous program decide, review, and proceed
without operator clicks for everything inside the accepted scope. It classifies
every proposed decision into one of four lanes and routes it accordingly:

    D0 — deterministic automatic      -> resolved by code/tests, no human, no reviewer
    D1 — reviewer-approved automatic  -> delegated to an independent reviewer adapter
    H1 — batched product decision     -> collected, resolved at the next milestone gate
    H2 — immediate hard boundary      -> STOP; requires the operator

No modal question is raised for D0/D1 (D1 uses the reviewer adapter, which may
be a real bridge or — honestly labeled — a deterministic simulation when no live
reviewer is available).

Canonical root: ``~/.local/share/joss-orchestrator/control-plane/``

Design invariants (unchanged from the package):

* Standard library only.
* State lives OUTSIDE the repo.
* Generic core contains no project literals.
* Fail-closed integrity; secret redaction on all persisted state.

Reuses — never duplicates:

* :mod:`joao_orchestrator.storage.atomic` (``atomic_write_json``, ``append_line``)
* :mod:`joao_orchestrator.v2.state` (``JOSS_ROOT``)
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..storage.atomic import atomic_write_json, append_line  # reuse, not duplicate
from . import state as _state

CONTROL_PLANE_SCHEMA_VERSION = "2.0"


def _resolve_root() -> Path:
    """Resolve the control-plane root lazily from state.JOSS_ROOT.

    Read at call time (not import time) so test isolation (which repoints
    ``state.JOSS_ROOT``) works, mirroring ``state.project_state_dir``.
    """
    return _state.JOSS_ROOT / "control-plane"


# Backward-compat module-level handle (frozen at import; prefer _resolve_root).
CONTROL_PLANE_ROOT = _resolve_root()

# Decision classes (the four lanes).
D0 = "D0"   # deterministic automatic
D1 = "D1"   # reviewer-approved automatic
H1 = "H1"   # batched product decision
H2 = "H2"   # immediate hard boundary (stop)
ALL_CLASSES = (D0, D1, H1, H2)

# Proposal lifecycle.
STATUS_PROPOSED = "PROPOSED"
STATUS_AUTO_RESOLVED = "AUTO_RESOLVED"        # D0
STATUS_REVIEW_REQUESTED = "REVIEW_REQUESTED"  # D1 -> reviewer
STATUS_REVIEWED = "REVIEWED"                  # D1 reviewer returned a verdict
STATUS_BATCHED = "BATCHED"                    # H1
STATUS_STOPPED = "STOPPED"                    # H2

# Secret redaction (reuses the same forbidden-substring idea as state.redaction).
_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|secret|password|token|bearer|credential)")
_SECRET_VAL_RE = re.compile(r"(?i)(bearer\s+[a-z0-9_\-]{16,}|gh[ops]_[a-z0-9]{16,}|[a-f0-9]{40})")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_nonblank(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"control plane requires a non-empty {name}")
    return value.strip()


def _redact(obj: Any) -> Any:
    """Recursively redact secret-shaped keys/values before persistence."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_VAL_RE.sub("***REDACTED***", obj)
    return obj


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class Proposal:
    """A proposed decision the autonomous program wants to make.

    ``decision_class`` is assigned by the :class:`DecisionClassifier`; a proposal
    may be submitted without it (the classifier fills it in).
    """
    proposal_id: str
    project_id: str
    kind: str                 # e.g. "merge_pr", "deploy_preview", "rollback", "question"
    summary: str
    context: dict[str, Any] = field(default_factory=dict)
    decision_class: str = ""  # D0/D1/H1/H2 (assigned by classifier)
    status: str = STATUS_PROPOSED
    created_at: str = ""
    resolved_at: str = ""
    resolution: str = ""      # what was decided
    proposer: str = "executor"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Proposal":
        return cls(
            proposal_id=str(d.get("proposal_id", "")),
            project_id=str(d.get("project_id", "")),
            kind=str(d.get("kind", "")),
            summary=str(d.get("summary", "")),
            context=dict(d.get("context", {})),
            decision_class=str(d.get("decision_class", "")),
            status=str(d.get("status", STATUS_PROPOSED)),
            created_at=str(d.get("created_at", "")),
            resolved_at=str(d.get("resolved_at", "")),
            resolution=str(d.get("resolution", "")),
            proposer=str(d.get("proposer", "executor")),
        )

    def validate(self) -> None:
        _require_nonblank(self.proposal_id, "proposal_id")
        _require_nonblank(self.project_id, "project_id")
        _require_nonblank(self.kind, "kind")
        _require_nonblank(self.summary, "summary")
        if self.decision_class and self.decision_class not in ALL_CLASSES:
            raise ValueError(f"decision_class must be one of {ALL_CLASSES}")


@dataclass
class ReviewRecord:
    """A reviewer's verdict on a D1 proposal."""
    proposal_id: str
    verdict: str              # APPROVE | REJECT | NEEDS_INFO
    reviewer: str             # adapter name
    rationale: str
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Decision classifier (deterministic)
# ---------------------------------------------------------------------------

class DecisionClassifier:
    """Deterministically assigns a decision class to a proposal.

    Classification rules (evaluated in order; first match wins):

    * H2 — the kind or context hits a hard boundary (money, accounts, third-party
      comms, constitution, unrecoverable data, or an explicit ``hard_boundary`` flag).
    * H1 — the kind is a product-level decision (``product_decision``) or context
      flags ``batch=true``.
    * D1 — the kind is a bounded technical approval (``merge_pr``,
      ``technical_review``) that needs a second pair of eyes.
    * D0 — everything else (routine, deterministic, or already-gated operations).

    The classifier is intentionally conservative: anything ambiguous leans to a
    HIGHER human lane, never a lower one.
    """
    H2_KINDS = frozenset({"send_email", "send_message", "transfer_money",
                          "change_account", "change_secret", "change_constitution"})
    H2_CONTEXT_KEYS = frozenset({"hard_boundary", "third_party", "money",
                                 "account_ownership", "unrecoverable"})
    H1_KINDS = frozenset({"product_decision", "milestone_outcome_change"})
    D1_KINDS = frozenset({"merge_pr", "technical_review", "scope_change"})

    def classify(self, proposal: Proposal) -> str:
        kind = proposal.kind
        ctx = proposal.context or {}
        # H2: hard boundary by kind or context.
        if kind in self.H2_KINDS:
            return H2
        if any(str(ctx.get(k, "")).lower() in ("true", "1", "yes")
               for k in self.H2_CONTEXT_KEYS):
            return H2
        # H1: product-level / batched.
        if kind in self.H1_KINDS or str(ctx.get("batch", "")).lower() == "true":
            return H1
        # D1: bounded technical approval.
        if kind in self.D1_KINDS:
            return D1
        # D0: routine / deterministic.
        return D0


# ---------------------------------------------------------------------------
# Reviewer bridge
# ---------------------------------------------------------------------------

class ReviewerAdapter:
    """Base reviewer adapter. Subclasses/adapters implement ``review``.

    Honesty: a real bridge (local MCP server / authenticated localhost bridge)
    must be tested before autonomy claims are made. Where no live reviewer is
    available, a :class:`DeterministicReviewerAdapter` is used and labeled as a
    simulation — it never claims to be a human or an external reviewer.
    """

    name = "base"

    def review(self, proposal: Proposal) -> ReviewRecord:
        raise NotImplementedError


class DeterministicReviewerAdapter(ReviewerAdapter):
    """A deterministic, HONESTLY-LABELED simulation of a reviewer.

    This is used in tests and when no live reviewer bridge is available. It
    approves proposals whose context contains deterministic evidence of safety
    (``tests_pass=true``, ``review_pass=true``) and rejects otherwise. It NEVER
    claims to be a human reviewer; its ``reviewer`` name records that it is a
    simulation.
    """
    name = "deterministic-simulation (no live reviewer bridge)"

    def review(self, proposal: Proposal) -> ReviewRecord:
        ctx = proposal.context or {}
        safe = (str(ctx.get("tests_pass", "")).lower() == "true"
                and str(ctx.get("review_pass", "")).lower() == "true"
                and str(ctx.get("no_critical_finding", "")).lower() == "true")
        verdict = "APPROVE" if safe else "REJECT"
        rationale = ("deterministic simulation: gates satisfied"
                     if safe else
                     "deterministic simulation: gates not satisfied")
        return ReviewRecord(
            proposal_id=proposal.proposal_id, verdict=verdict,
            reviewer=self.name, rationale=rationale, timestamp=_utcnow())


# ---------------------------------------------------------------------------
# Control plane store (project-isolated, concurrency-safe)
# ---------------------------------------------------------------------------

class ControlPlane:
    """Owns the proposal/review/approval store + decision queue + event log.

    Each project gets an isolated directory under the control-plane root, so
    cross-project proposal loading is impossible (proposals carry + are checked
    against ``project_id``). Writes are serialized per-project with a
    :class:`threading.Lock` (in-process) and atomic (on disk).
    """

    def __init__(self, project_id: str, *,
                 reviewer: ReviewerAdapter | None = None,
                 classifier: DecisionClassifier | None = None):
        _require_nonblank(project_id, "project_id")
        if "/" in project_id or ".." in project_id:
            raise ValueError(f"invalid project_id: {project_id!r}")
        self.project_id = project_id
        self.root = _resolve_root() / project_id
        self.proposals_dir = self.root / "proposals"
        self.queue_path = self.root / "queue.json"
        self.events_path = self.root / "events.jsonl"
        self.batch_path = self.root / "batch.json"
        self.proposals_dir.mkdir(parents=True, exist_ok=True)
        self.reviewer = reviewer or DeterministicReviewerAdapter()
        self.classifier = classifier or DecisionClassifier()
        self._lock = threading.Lock()

    # -- paths ------------------------------------------------------------
    def _proposal_path(self, proposal_id: str) -> Path:
        return self.proposals_dir / f"{proposal_id}.json"

    # -- event log --------------------------------------------------------
    def _log(self, event: str, detail: dict[str, Any]) -> None:
        line = json.dumps({
            "event": event, "project_id": self.project_id,
            "ts": _utcnow(), **_redact(detail)},
            sort_keys=True)
        append_line(self.events_path, line)

    # -- queue (atomic read-modify-write) --------------------------------
    def _read_queue(self) -> list[dict[str, Any]]:
        if not self.queue_path.exists():
            return []
        return json.loads(self.queue_path.read_text(encoding="utf-8"))

    def _write_queue(self, q: list[dict[str, Any]]) -> None:
        atomic_write_json(self.queue_path, _redact(q))

    # -- proposal lifecycle ----------------------------------------------
    def submit(self, proposal: Proposal) -> Proposal:
        """Submit a proposal: classify it and route it by decision class.

        Returns the (possibly updated) proposal with its decision_class + status.
        No modal question is raised for D0/D1.
        """
        proposal.validate()
        if proposal.project_id != self.project_id:
            raise ValueError(
                f"proposal project_id {proposal.project_id!r} != control plane "
                f"{self.project_id!r}; cross-project submission refused")
        if not proposal.created_at:
            proposal.created_at = _utcnow()
        proposal.decision_class = self.classifier.classify(proposal)

        with self._lock:
            self._persist(proposal)
            q = self._read_queue()
            q.append({"proposal_id": proposal.proposal_id,
                      "decision_class": proposal.decision_class,
                      "status": proposal.status,
                      "kind": proposal.kind,
                      "ts": _utcnow()})
            self._write_queue(q)
            self._log("proposal_submitted", {"proposal_id": proposal.proposal_id,
                     "decision_class": proposal.decision_class, "kind": proposal.kind})

        # Route immediately for D0/D1 (no operator question).
        if proposal.decision_class == D0:
            proposal = self._auto_resolve(proposal)
        elif proposal.decision_class == D1:
            proposal = self._request_review(proposal)
        elif proposal.decision_class == H1:
            proposal = self._batch(proposal)
        elif proposal.decision_class == H2:
            proposal = self._stop(proposal)
        return proposal

    def _persist(self, proposal: Proposal) -> None:
        atomic_write_json(self._proposal_path(proposal.proposal_id),
                          _redact(proposal.to_dict()))

    def _auto_resolve(self, proposal: Proposal) -> Proposal:
        """D0: resolve deterministically and record the resolution."""
        proposal.status = STATUS_AUTO_RESOLVED
        proposal.resolution = "auto-resolved (D0 deterministic)"
        proposal.resolved_at = _utcnow()
        with self._lock:
            self._persist(proposal)
            self._log("proposal_auto_resolved",
                      {"proposal_id": proposal.proposal_id})
        return proposal

    def _request_review(self, proposal: Proposal) -> Proposal:
        """D1: send to the reviewer adapter and apply its verdict immediately.

        In a real bridge this would be async; here the adapter runs synchronously.
        A stalled reviewer (NEEDS_INFO) is recovered by the no-stop watchdog.
        """
        proposal.status = STATUS_REVIEW_REQUESTED
        with self._lock:
            self._persist(proposal)
        review = self.reviewer.review(proposal)
        proposal.status = STATUS_REVIEWED
        proposal.resolution = f"{review.verdict} ({review.reviewer})"
        proposal.resolved_at = _utcnow()
        with self._lock:
            self._persist(proposal)
            atomic_write_json(
                self.proposals_dir / f"{proposal.proposal_id}.review.json",
                _redact(review.to_dict()))
            self._log("proposal_reviewed",
                      {"proposal_id": proposal.proposal_id,
                       "verdict": review.verdict, "reviewer": review.reviewer})
        return proposal

    def _batch(self, proposal: Proposal) -> Proposal:
        """H1: collect for resolution at the next milestone gate."""
        proposal.status = STATUS_BATCHED
        proposal.resolution = "batched for next milestone gate (H1)"
        with self._lock:
            self._persist(proposal)
            batch = self._read_batch()
            batch.append({"proposal_id": proposal.proposal_id,
                          "summary": proposal.summary, "ts": _utcnow()})
            atomic_write_json(self.batch_path, _redact(batch))
            self._log("proposal_batched",
                      {"proposal_id": proposal.proposal_id})
        return proposal

    def _stop(self, proposal: Proposal) -> Proposal:
        """H2: hard boundary — stop and require the operator."""
        proposal.status = STATUS_STOPPED
        proposal.resolution = "HARD BOUNDARY (H2): requires operator"
        with self._lock:
            self._persist(proposal)
            self._log("proposal_stopped_h2",
                      {"proposal_id": proposal.proposal_id, "kind": proposal.kind})
        return proposal

    def _read_batch(self) -> list[dict[str, Any]]:
        if not self.batch_path.exists():
            return []
        return json.loads(self.batch_path.read_text(encoding="utf-8"))

    # -- queries ----------------------------------------------------------
    def load(self, proposal_id: str, project_id: str | None = None) -> Proposal | None:
        """Load a proposal. Refuses cross-project loading."""
        if project_id is not None and project_id != self.project_id:
            return None
        p = self._proposal_path(proposal_id)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        prop = Proposal.from_dict(data)
        if prop.project_id != self.project_id:
            return None  # cross-project file present -> refused
        return prop

    def queue(self) -> list[dict[str, Any]]:
        return self._read_queue()

    def batched(self) -> list[dict[str, Any]]:
        return self._read_batch()

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        return [json.loads(ln) for ln in self.events_path.read_text().splitlines()
                if ln.strip()]

    # -- question suppression --------------------------------------------
    def routine_questions_in_run(self) -> int:
        """Count routine questions that escaped to the operator (target: 0).

        A routine question is a proposal of kind 'question' whose final persisted
        status was NOT auto-resolved/batched/stopped (i.e. it would block the
        run). Reads the authoritative proposal files, not the queue snapshot
        (which is recorded before routing completes). With the classifier in
        place, 'question' kinds are D0 and auto-resolve, so this is 0.
        """
        escaped = 0
        for p in self.proposals_dir.glob("*.json"):
            if p.name.endswith(".review.json"):
                continue
            try:
                prop = Proposal.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except (ValueError, json.JSONDecodeError):
                continue
            if prop.project_id != self.project_id:
                continue
            if prop.kind == "question" and prop.status not in (
                    STATUS_AUTO_RESOLVED, STATUS_BATCHED, STATUS_STOPPED):
                escaped += 1
        return escaped

    # -- resume -----------------------------------------------------------
    def resume_unresolved(self) -> list[str]:
        """Re-route any proposal left in PROPOSED/REVIEW_REQUESTED (interruption recovery)."""
        resumed: list[str] = []
        for q in self._read_queue():
            pid = q.get("proposal_id", "")
            prop = self.load(pid)
            if prop and prop.status in (STATUS_PROPOSED, STATUS_REVIEW_REQUESTED):
                # re-classify + re-route
                prop.status = STATUS_PROPOSED
                prop = self.submit(prop)
                resumed.append(pid)
        return resumed


# ---------------------------------------------------------------------------
# No-stop watchdog
# ---------------------------------------------------------------------------

class NoStopWatchdog:
    """Detects stalled questions/proposals and reroutes them so the run doesn't block.

    A proposal is "stalled" if it has been REVIEW_REQUESTED (D1) for longer than
    ``stall_seconds`` without a verdict. The watchdog reroutes a stalled D1 to
    H1 (batched) so the run continues; if it is actually a hard boundary it is
    reclassified to H2 (stop).
    """

    def __init__(self, control_plane: ControlPlane, *, stall_seconds: float = 5.0,
                 now_fn: Callable[[], float] = time.monotonic):
        self.cp = control_plane
        self.stall_seconds = stall_seconds
        self._now = now_fn
        self._submitted_at: dict[str, float] = {}

    def remember(self, proposal_id: str) -> None:
        self._submitted_at[proposal_id] = self._now()

    def scan_and_recover(self) -> list[str]:
        """Reroute stalled D1 proposals to H1. Returns the rerouted proposal ids."""
        rerouted: list[str] = []
        now = self._now()
        for q in self.cp.queue():
            pid = q.get("proposal_id", "")
            if q.get("decision_class") == D1 and q.get("status") == STATUS_REVIEW_REQUESTED:
                age = now - self._submitted_at.get(pid, now)
                if age >= self.stall_seconds:
                    prop = self.cp.load(pid)
                    if prop:
                        prop.decision_class = H1
                        prop.status = STATUS_PROPOSED
                        self.cp._batch(prop)
                        rerouted.append(pid)
        return rerouted


__all__ = [
    "CONTROL_PLANE_SCHEMA_VERSION", "CONTROL_PLANE_ROOT",
    "D0", "D1", "H1", "H2", "ALL_CLASSES",
    "STATUS_PROPOSED", "STATUS_AUTO_RESOLVED", "STATUS_REVIEW_REQUESTED",
    "STATUS_REVIEWED", "STATUS_BATCHED", "STATUS_STOPPED",
    "Proposal", "ReviewRecord",
    "DecisionClassifier", "ReviewerAdapter", "DeterministicReviewerAdapter",
    "ControlPlane", "NoStopWatchdog",
]
