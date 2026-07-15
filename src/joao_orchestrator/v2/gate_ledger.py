"""V2 gate ledger — immutable passed gates + contradiction invalidation (§9).

A gate is a verifiable checkpoint in a roadmap. Once a gate PASSES with
evidence, that outcome is **immutable**: it cannot be re-evaluated to a
non-PASS state unless a structured *contradiction record* is supplied that
names the affected gate, the mismatch, the evidence source, and the command
that reproduces it.

This is the single mechanism that makes "silent gate reopening impossible"
(§34 success criterion). It directly prevents historical failure #4
("Merged PR treated as open") and #5 ("Passed gates reopened without
contradiction").

Storage: append-only JSONL at
``~/.local/share/joss-orchestrator/projects/<id>/state/gate_ledger.jsonl``.

Reuses :mod:`joao_orchestrator.storage.atomic` append_line (fsync'd append).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..storage.atomic import append_line  # reuse, not duplicate
from .state import project_state_dir

LEDGER_FILENAME = "gate_ledger.jsonl"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_path(project_id: str) -> Path:
    return project_state_dir(project_id) / LEDGER_FILENAME


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

class GateStatus:
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    INVALIDATED = "INVALIDATED"


@dataclass
class GateRecord:
    """One immutable gate outcome line in the ledger."""
    gate_id: str
    status: str                       # PASSED | BLOCKED | INVALIDATED
    evidence: list[str] = field(default_factory=list)
    source_run_id: str = ""
    head: str = ""
    timestamp: str = ""
    reason: str = ""
    contradiction_id: str | None = None   # set when an INVALIDATED record

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "GateRecord":
        return cls(
            gate_id=str(d["gate_id"]),
            status=str(d["status"]),
            evidence=list(d.get("evidence", [])),
            source_run_id=str(d.get("source_run_id", "")),
            head=str(d.get("head", "")),
            timestamp=str(d.get("timestamp", "")),
            reason=str(d.get("reason", "")),
            contradiction_id=d.get("contradiction_id"),
        )


@dataclass
class ContradictionRecord:
    """Structured evidence that a previously-PASSED gate must be invalidated.

    Required fields (§9): source, timestamp, mismatch description, the
    affected gate, confidence, and a verification command that reproduces the
    mismatch. A contradiction with no verification command is refused.
    """
    contradiction_id: str
    affected_gate_id: str
    source: str                 # e.g. "live_command", "git", "http_check"
    mismatch: str
    evidence: list[str] = field(default_factory=list)
    confidence: str = "HIGH"    # HIGH required to invalidate a passed gate
    verification_command: str = ""
    timestamp: str = ""

    def validate(self) -> None:
        # m6 closure: validate ALL identifying fields, not just two. A
        # contradiction that is missing its id, the affected gate, its source,
        # or a non-empty mismatch description must be refused — otherwise an
        # empty/partial record could silently invalidate a passed gate.
        for field_name in ("contradiction_id", "affected_gate_id",
                           "source", "mismatch"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"contradiction requires a non-empty {field_name} (§9)")
        if not self.verification_command.strip():
            raise ValueError(
                "contradiction requires a verification_command that "
                "reproduces the mismatch (§9)"
            )
        if self.confidence != "HIGH":
            raise ValueError(
                "only HIGH-confidence contradictions may invalidate a "
                "passed gate (§9)"
            )


class GateImmutabilityError(ValueError):
    """Raised when a mutation would reopen an immutable PASSED gate (§9/§34).

    This is a subclass of :class:`ValueError` so existing callers that catch
    ``ValueError`` (e.g. around ``invalidate``) keep working, while making the
    immutability violation distinguishable for new callers.
    """


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class GateLedger:
    """Append-only gate ledger with immutable PASSED records."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.path = ledger_path(project_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- low level --------------------------------------------------------
    def _append(self, record: GateRecord) -> None:
        append_line(self.path, json.dumps(record.to_dict(), sort_keys=True))

    def read_all(self) -> list[GateRecord]:
        """Return every record in ledger order (oldest first)."""
        if not self.path.exists():
            return []
        out: list[GateRecord] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                out.append(GateRecord.from_dict(json.loads(line)))
        return out

    # -- the core invariant ----------------------------------------------
    def passed_gates(self) -> list[str]:
        """Return gate_ids whose latest NON-invalidated status is PASSED.

        A gate that PASSED and was later INVALIDATED by a contradiction is
        NOT counted as passed. This is the one, controlled way a passed gate
        can be reopened — and it always emits an INVALIDATED ledger line and
        a contradiction record first.
        """
        latest: dict[str, GateRecord] = {}
        for rec in self.read_all():
            latest[rec.gate_id] = rec
        # A gate is "currently passed" iff its latest record is PASSED.
        # (INVALIDATED records have the same gate_id, so they supersede.)
        return sorted(gid for gid, rec in latest.items()
                      if rec.status == GateStatus.PASSED)

    def is_passed(self, gate_id: str) -> bool:
        """True iff ``gate_id``'s latest ledger status is PASSED."""
        return gate_id in self.passed_gates()

    # -- writing ----------------------------------------------------------
    def record_pass(
        self, gate_id: str, *, evidence: Iterable[str] = (),
        source_run_id: str = "", head: str = "", reason: str = "",
    ) -> GateRecord:
        """Record a PASS for ``gate_id``.

        Idempotent: re-passing an already-passed gate (with no intervening
        invalidation) is a no-op that does NOT append a duplicate line — this
        prevents historical failure #5 (reopened gates) by making spurious
        re-evaluation cheap and harmless, while real reopening still requires
        a contradiction.
        """
        if gate_id in self.passed_gates():
            # Already passed and not invalidated: do not duplicate.
            return GateRecord(
                gate_id=gate_id, status=GateStatus.PASSED,
                evidence=list(evidence), source_run_id=source_run_id,
                head=head, timestamp=_utcnow(), reason="already_passed",
            )
        rec = GateRecord(
            gate_id=gate_id, status=GateStatus.PASSED,
            evidence=list(evidence), source_run_id=source_run_id,
            head=head, timestamp=_utcnow(),
            reason=reason or "gate verification succeeded",
        )
        self._append(rec)
        return rec

    def record_block(
        self, gate_id: str, *, reason: str, evidence: Iterable[str] = (),
        source_run_id: str = "", head: str = "",
    ) -> GateRecord:
        """Record a BLOCK for ``gate_id``.

        A gate that is currently PASSED is **immutable** (§9/§34): it can be
        reopened only via :meth:`invalidate` with a valid HIGH-confidence
        :class:`ContradictionRecord`. ``record_block`` is therefore refused on
        a PASSED gate — it neither appends a BLOCKED line nor removes the gate
        from the passed set. Blocks remain mutable by design for gates that
        are not currently passed (e.g. a fresh BLOCKED gate, or one already
        invalidated by a contradiction).
        """
        if gate_id in self.passed_gates():
            raise GateImmutabilityError(
                f"gate {gate_id!r} is currently PASSED; a passed gate cannot "
                "transition through record_block (§9). Reopen it via "
                "invalidate() with a HIGH-confidence ContradictionRecord that "
                "carries a verification_command."
            )
        rec = GateRecord(
            gate_id=gate_id, status=GateStatus.BLOCKED,
            evidence=list(evidence), source_run_id=source_run_id,
            head=head, timestamp=_utcnow(), reason=reason,
        )
        self._append(rec)
        return rec

    def invalidate(
        self, contradiction: ContradictionRecord, *, source_run_id: str = "",
        head: str = "",
    ) -> GateRecord:
        """Invalidate a previously-PASSED gate using a contradiction record.

        Refused unless the gate is currently passed (you cannot invalidate a
        gate that is not passed) and the contradiction is HIGH-confidence
        with a verification command (§9).
        """
        contradiction.validate()
        if contradiction.affected_gate_id not in self.passed_gates():
            raise ValueError(
                f"gate {contradiction.affected_gate_id!r} is not currently "
                "passed; nothing to invalidate"
            )
        # 1. record the contradiction itself as an INVALIDATED line.
        rec = GateRecord(
            gate_id=contradiction.affected_gate_id,
            status=GateStatus.INVALIDATED,
            evidence=contradiction.evidence,
            source_run_id=source_run_id, head=head,
            timestamp=contradiction.timestamp or _utcnow(),
            reason=(
                f"INVALIDATED by contradiction {contradiction.contradiction_id}: "
                f"{contradiction.mismatch} "
                f"(source={contradiction.source}, "
                f"verify=`{contradiction.verification_command}`)"
            ),
            contradiction_id=contradiction.contradiction_id,
        )
        self._append(rec)
        return rec


__all__ = [
    "GateStatus", "GateRecord", "ContradictionRecord", "GateImmutabilityError",
    "GateLedger", "ledger_path",
]
