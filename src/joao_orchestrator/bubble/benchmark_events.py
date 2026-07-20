"""Product Superiority & Efficiency instrumentation (C8-B condition of GO,
`JOAO_C8_GATES_SPEC.md` §24, `JOAO_C8_GATES_ROADMAP.md` LOT C8-B). This is
NOT a technical gate — it never blocks a mission — but it is required so
C8-C can later measure §24.

Observes the SAME real entrypoints the C8-B gates already use (never a
duplicated dispatch path — G-NO-STALE-ENTRYPOINT applies here too, just as
much as to the builder/reviewer adapters themselves).

Two artefacts:
  `benchmark-run-manifest.json` — one per benchmark run: identifies
      base_sha/spec_sha/roadmap_sha/risk_tier/workflow_mode.
  `benchmark-events.jsonl`      — append-only, hash-chained event log.

Determinism claim is about CALCULATION, never model behavior: the SAME event
log always serializes/derives the SAME metrics; two EXECUTIONS of an
agent/LLM may legitimately differ in call count/retries/actions while both
producing a correct result — that variance is never treated as a defect.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..storage.atomic import append_line, atomic_write_json

SCHEMA_VERSION = 1
WORKFLOW_MODES = ("manual_same_stack", "manual_current_workflow", "joao")
GENESIS_HASH = "0" * 64
UNKNOWN = "unknown"


class BenchmarkInstrumentationError(RuntimeError):
    pass


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def write_run_manifest(events_dir: Path, *, benchmark_id: str, run_id: str, mission_id: str,
                       workflow_mode: str, base_sha: str, spec_sha: str, roadmap_sha: str,
                       risk_tier: str, randomized_order: bool = False) -> dict[str, Any]:
    if workflow_mode not in WORKFLOW_MODES:
        raise BenchmarkInstrumentationError(f"workflow_mode must be one of {WORKFLOW_MODES}, got {workflow_mode!r}")
    manifest = {
        "schema_version": SCHEMA_VERSION, "benchmark_id": benchmark_id, "run_id": run_id,
        "mission_id": mission_id, "workflow_mode": workflow_mode, "base_sha": base_sha,
        "spec_sha": spec_sha, "roadmap_sha": roadmap_sha, "risk_tier": risk_tier,
        "randomized_order": bool(randomized_order),
        # §24.5 acceptance criteria, tracked from the start rather than
        # bolted on after the fact: 100 unless a later instrumentation
        # failure downgrades it (mark_evidence_incomplete, below); never a
        # logged secret in this file or the event log (SECRETS_LOGGED=0 is
        # a structural claim — this module never accepts/writes prompt
        # content or credentials, only event metadata).
        "evidence_completeness": 100, "incompleteness_reasons": [],
        "secrets_logged": 0, "raw_event_log_size_bytes": 0,
    }
    atomic_write_json(Path(events_dir) / "benchmark-run-manifest.json", manifest)
    return manifest


def mark_evidence_incomplete(events_dir: Path, reason: str) -> dict[str, Any]:
    """§24.5: an instrumentation failure must NEVER break the product run —
    it is recorded here as reduced evidence completeness instead."""
    path = Path(events_dir) / "benchmark-run-manifest.json"
    manifest = json.loads(path.read_text()) if path.is_file() else {"evidence_completeness": 100,
                                                                     "incompleteness_reasons": []}
    manifest["evidence_completeness"] = 99 if manifest.get("evidence_completeness", 100) >= 99 else manifest["evidence_completeness"]
    manifest.setdefault("incompleteness_reasons", []).append(reason)
    atomic_write_json(path, manifest)
    return manifest


def update_raw_log_size(events_dir: Path, events_path: Path) -> None:
    path = Path(events_dir) / "benchmark-run-manifest.json"
    if not path.is_file():
        return
    manifest = json.loads(path.read_text())
    manifest["raw_event_log_size_bytes"] = Path(events_path).stat().st_size if Path(events_path).is_file() else 0
    atomic_write_json(path, manifest)


def usage_fields(usage: dict[str, Any] | None, *, role: str) -> dict[str, Any]:
    """`role` in {"builder","reviewer","orchestration"}. A provider that
    exposes no usage data yields the literal string "unknown" for every
    field — never a fabricated `0` (§24.5: "ne rapporte jamais une valeur de
    token inventée")."""
    usage = usage or {}
    return {
        f"{role}_input_tokens": usage.get("input", UNKNOWN),
        f"{role}_output_tokens": usage.get("output", UNKNOWN),
        f"{role}_cache_read_tokens": usage.get("cache_read", UNKNOWN),
        f"{role}_cache_write_tokens": usage.get("cache_write", UNKNOWN),
    }


class EventLog:
    """Append-only, hash-chained benchmark event log
    (`benchmark-events.jsonl`). Single-writer per `events_path`, matching
    every other JOAO evidence file's concurrency model."""

    def __init__(self, events_path: Path, *, benchmark_id: str, run_id: str, mission_id: str,
                workflow_mode: str, base_sha: str, spec_sha: str, roadmap_sha: str):
        if workflow_mode not in WORKFLOW_MODES:
            raise BenchmarkInstrumentationError(f"workflow_mode must be one of {WORKFLOW_MODES}, got {workflow_mode!r}")
        self.events_path = Path(events_path)
        self._identity = {
            "benchmark_id": benchmark_id, "run_id": run_id, "mission_id": mission_id,
            "workflow_mode": workflow_mode, "base_sha": base_sha, "spec_sha": spec_sha,
            "roadmap_sha": roadmap_sha,
        }
        self._sequence = 0
        self._previous_hash = GENESIS_HASH
        self._candidate_bound = False
        if self.events_path.exists():
            for line in self.events_path.read_text().splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                self._sequence = max(self._sequence, event.get("sequence_number", 0))
                self._previous_hash = event.get("event_hash", self._previous_hash)
                if event.get("event_type") == "CANDIDATE_BOUND":
                    self._candidate_bound = True

    def append(self, event_type: str, *, candidate_tree: str | None = None,
              candidate_commit: str | None = None, timestamp: str, **fields: Any) -> dict[str, Any]:
        """Append one event. `timestamp` is caller-supplied (e.g.
        `runtime.now()`) — this module never calls a clock itself, so
        callers stay in control of the clock source and this stays testable
        without patching global time.

        Fail-closed candidate binding: before `CANDIDATE_BOUND` is ever
        appended, `candidate_tree`/`candidate_commit` MUST be None; the
        `CANDIDATE_BOUND` event itself MUST carry a real, non-empty pair;
        every event after it MUST carry a real, non-empty `candidate_tree`
        — never silently None post-bind (candidate identity is never
        implicit once it exists).
        """
        if event_type == "CANDIDATE_BOUND":
            if not candidate_tree or not candidate_commit:
                raise BenchmarkInstrumentationError(
                    "CANDIDATE_BOUND requires a real, non-empty candidate_tree and candidate_commit")
        elif not self._candidate_bound:
            if candidate_tree is not None or candidate_commit is not None:
                raise BenchmarkInstrumentationError(
                    "candidate_tree/candidate_commit must be null before CANDIDATE_BOUND has been emitted")
        else:
            if not candidate_tree:
                raise BenchmarkInstrumentationError(
                    f"event {event_type!r} is emitted after CANDIDATE_BOUND and requires a real "
                    "candidate_tree — a null/missing value here is refused, never silently accepted")

        self._sequence += 1
        body = {
            "schema_version": SCHEMA_VERSION, **self._identity, "sequence_number": self._sequence,
            "event_type": event_type, "at": timestamp,
            "candidate_tree": candidate_tree, "candidate_commit": candidate_commit,
            "previous_event_hash": self._previous_hash,
            **fields,
        }
        event_hash = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
        event = {**body, "event_hash": event_hash}
        append_line(self.events_path, json.dumps(event, sort_keys=True))
        self._previous_hash = event_hash
        if event_type == "CANDIDATE_BOUND":
            self._candidate_bound = True
        return event


def verify_chain(events_path: Path) -> dict[str, Any]:
    """Independently re-verify the append-only hash chain — never trusts
    that the file wasn't tampered with after the fact. Detects: a deleted
    event (sequence gap), a reordered/edited event (hash mismatch), a reused
    sequence_number, and a `previous_event_hash` that does not match the
    prior event's actual `event_hash`."""
    events_path = Path(events_path)
    if not events_path.exists():
        return {"ok": False, "reason": "event log does not exist", "event_count": 0}
    events = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except (json.JSONDecodeError, ValueError) as exc:
            return {"ok": False, "reason": f"unparseable event line: {exc}", "event_count": len(events)}

    previous_hash = GENESIS_HASH
    seen_sequences: set[int] = set()
    candidate_bound = False
    for index, event in enumerate(events):
        seq = event.get("sequence_number")
        if not isinstance(seq, int) or seq != index + 1:
            return {"ok": False, "reason": f"sequence_number gap or reuse at position {index} (got {seq!r}, "
                                          f"expected {index + 1})", "event_count": len(events)}
        if seq in seen_sequences:
            return {"ok": False, "reason": f"sequence_number {seq} reused", "event_count": len(events)}
        seen_sequences.add(seq)

        stored_hash = event.get("event_hash")
        stored_previous = event.get("previous_event_hash")
        if stored_previous != previous_hash:
            return {"ok": False, "reason": f"event {seq}: previous_event_hash {stored_previous!r} does not "
                                          f"match the prior event's actual hash {previous_hash!r} — chain broken",
                    "event_count": len(events)}
        body = {k: v for k, v in event.items() if k != "event_hash"}
        recomputed = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
        if recomputed != stored_hash:
            return {"ok": False, "reason": f"event {seq}: stored event_hash does not match a fresh recompute "
                                          "— this event was modified after it was appended",
                    "event_count": len(events)}
        if event.get("event_type") == "CANDIDATE_BOUND":
            candidate_bound = True
        elif candidate_bound and not event.get("candidate_tree"):
            return {"ok": False, "reason": f"event {seq} ({event.get('event_type')}) has no candidate_tree "
                                          "despite following CANDIDATE_BOUND", "event_count": len(events)}
        previous_hash = stored_hash

    return {"ok": True, "reason": "hash chain verified, sequence numbers contiguous, candidate binding intact",
            "event_count": len(events)}


def derive_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure function of the event list: same input list -> same output,
    always (determinism of CALCULATION — never a claim that two separate
    agent executions produce the same event list in the first place).
    Deliberately simple for C8-B MVP: event-type counts plus first/last
    timestamps; §24's fuller efficiency metrics are C8-C+ scope."""
    counts: dict[str, int] = {}
    for event in events:
        kind = event.get("event_type", "UNKNOWN")
        counts[kind] = counts.get(kind, 0) + 1
    timestamps = [event.get("at") for event in events if event.get("at")]
    return {
        "total_events": len(events),
        "event_type_counts": dict(sorted(counts.items())),
        "first_event_at": timestamps[0] if timestamps else None,
        "last_event_at": timestamps[-1] if timestamps else None,
    }
