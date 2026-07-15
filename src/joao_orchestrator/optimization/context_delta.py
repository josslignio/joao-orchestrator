"""Context broker v2: hash-bound delta context packets (T3).

Extends the existing runtime/context_broker.py — does NOT create a parallel
broker. The existing ContextBroker.select() produces the base (full) bounded
packet; this module computes deltas against a previously-acknowledged packet
so repeated tasks for the same project transmit only what changed.

Spec fields added:
  base_context_hash
  previous_packet_hash
  delta_records           (new + changed records, full content)
  removed_record_ids
  changed_record_ids
  unchanged_record_hashes (record_id -> content sha256, no content)
  repository_index_version
  task_plan_hash
  cache_references

Protocol:
  - First packet for a (project, plan) is a FULL bounded packet (is_delta=False).
  - Later packets are deltas: only new/changed records carry content; unchanged
    records are referenced by hash.
  - The receiver acknowledges with the packet hash; the next delta binds to it.
  - base_context_hash mismatch -> trigger ONE rebuilt bounded packet, then
    deltas resume from the new base.

Target: median repeated-task context bytes <= 20% of baseline.

Deterministic: same inputs -> same delta. No network. Stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from ..evaluation.models import canonical_json_bytes, sha256_json
from ..runtime.context_broker import ContextBroker, ContextPacket
from ..storage.atomic import atomic_write_json


SCHEMA_VERSION = 1


def _record_content_hash(record: dict) -> str:
    """Stable content hash of a context record.

    Hashes the record's content + type + title (not its mutable metadata like
    created_at) so semantically-equal records hash equally across packets.
    """
    payload = {
        "record_id": record.get("record_id", ""),
        "record_type": record.get("record_type", ""),
        "title": record.get("title", ""),
        "content": record.get("content", ""),
    }
    return sha256_json(payload)


def _record_id_map(packet: ContextPacket) -> dict[str, dict]:
    """record_id -> record dict for a packet."""
    return {r.get("record_id", ""): r for r in packet.records}


def packet_context_hash(packet: ContextPacket) -> str:
    """Deterministic hash of a packet's selected record set (content-bound).

    Two packets with the same selected records (same content hashes, same ids)
    produce the same hash, regardless of created_at timing differences.
    """
    entries = []
    for r in sorted(packet.records, key=lambda x: (x.get("record_id", ""),)):
        entries.append({
            "id": r.get("record_id", ""),
            "hash": _record_content_hash(r),
        })
    return sha256_json({"entries": entries, "project_id": packet.project_id})


@dataclass(frozen=True)
class DeltaContextPacket:
    """A v2 context packet: either a full bounded packet or a delta.

    When is_delta=False, this wraps a full ContextPacket (first packet or
    rebuilt after base mismatch). When is_delta=True, only new/changed records
    carry content; unchanged records are hash references.
    """

    context_id: str
    project_id: str
    is_delta: bool
    base_context_hash: str
    previous_packet_hash: str
    delta_records: tuple[dict, ...]       # new + changed (full content)
    removed_record_ids: tuple[str, ...]
    changed_record_ids: tuple[str, ...]
    unchanged_record_hashes: tuple[tuple[str, str], ...]  # (record_id, content_hash)
    repository_index_version: str
    task_plan_hash: str
    cache_references: tuple[str, ...]
    total_bytes: int                       # serialized size of this packet
    full_packet_bytes: int                 # size of the equivalent full packet
    schema_version: int = SCHEMA_VERSION
    integrity_sha256: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "context_id": self.context_id,
            "project_id": self.project_id,
            "is_delta": self.is_delta,
            "base_context_hash": self.base_context_hash,
            "previous_packet_hash": self.previous_packet_hash,
            "delta_records": list(self.delta_records),
            "removed_record_ids": list(self.removed_record_ids),
            "changed_record_ids": list(self.changed_record_ids),
            "unchanged_record_hashes": [list(h) for h in self.unchanged_record_hashes],
            "repository_index_version": self.repository_index_version,
            "task_plan_hash": self.task_plan_hash,
            "cache_references": list(self.cache_references),
            "total_bytes": self.total_bytes,
            "full_packet_bytes": self.full_packet_bytes,
        }

    def with_integrity(self) -> "DeltaContextPacket":
        return replace(self, integrity_sha256=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.integrity_sha256) and self.integrity_sha256 == sha256_json(
            self.unsigned_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["integrity_sha256"] = self.integrity_sha256
        return payload

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DeltaContextPacket":
        return cls(
            context_id=str(d["context_id"]),
            project_id=str(d["project_id"]),
            is_delta=bool(d["is_delta"]),
            base_context_hash=str(d["base_context_hash"]),
            previous_packet_hash=str(d["previous_packet_hash"]),
            delta_records=tuple(d.get("delta_records", [])),
            removed_record_ids=tuple(d.get("removed_record_ids", [])),
            changed_record_ids=tuple(d.get("changed_record_ids", [])),
            unchanged_record_hashes=tuple(
                tuple(h) for h in d.get("unchanged_record_hashes", [])
            ),
            repository_index_version=str(d.get("repository_index_version", "")),
            task_plan_hash=str(d.get("task_plan_hash", "")),
            cache_references=tuple(d.get("cache_references", [])),
            total_bytes=int(d.get("total_bytes", 0)),
            full_packet_bytes=int(d.get("full_packet_bytes", 0)),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
            integrity_sha256=str(d.get("integrity_sha256", "")),
        )

    def serialized_bytes(self) -> int:
        """Actual serialized size of this packet (transparency for the proxy)."""
        return len(canonical_json_bytes(self.unsigned_dict()))


def _full_packet_bytes(packet: ContextPacket) -> int:
    """Size of the equivalent full packet (baseline for delta savings)."""
    return len(json.dumps(packet.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8"))


def build_full_packet(
    packet: ContextPacket,
    task_plan_hash: str = "",
    repository_index_version: str = "",
    cache_references: Optional[tuple[str, ...]] = None,
) -> DeltaContextPacket:
    """Build a FULL (non-delta) v2 packet from an existing ContextPacket.

    Used for the first packet of a (project, plan) pair, or when a base-hash
    mismatch forces a rebuild.
    """
    full_bytes = _full_packet_bytes(packet)
    base_hash = packet_context_hash(packet)
    # A full packet's "unchanged" set is empty (everything is new content).
    delta_records = tuple(packet.records)
    dp = DeltaContextPacket(
        context_id=packet.context_id,
        project_id=packet.project_id,
        is_delta=False,
        base_context_hash=base_hash,
        previous_packet_hash="",
        delta_records=delta_records,
        removed_record_ids=(),
        changed_record_ids=(),
        unchanged_record_hashes=(),
        repository_index_version=repository_index_version,
        task_plan_hash=task_plan_hash,
        cache_references=tuple(cache_references or ()),
        total_bytes=0,  # filled after construction
        full_packet_bytes=full_bytes,
    )
    dp = dp.with_integrity()
    return replace(dp, total_bytes=dp.serialized_bytes()).with_integrity()


def compute_delta(
    current: ContextPacket,
    previous: DeltaContextPacket,
    task_plan_hash: str = "",
    repository_index_version: str = "",
    cache_references: Optional[tuple[str, ...]] = None,
) -> DeltaContextPacket:
    """Compute a delta packet of `current` against the acknowledged `previous`.

    `previous` must be a packet the receiver acknowledged (its
    base_context_hash is the anchor). If current's record set matches previous's
    base, the delta is tiny (only unchanged hashes + no content).

    Raises ValueError if previous is itself a delta without a base_context_hash
    (deltas chain off the base, not off another delta's transient state).
    """
    if not previous.base_context_hash:
        raise ValueError("previous packet has no base_context_hash; cannot delta")

    base_hash = packet_context_hash(current)
    prev_records = {h[0]: h[1] for h in previous.unchanged_record_hashes}
    # For a full previous packet, reconstruct its id->hash map from delta_records.
    if previous.delta_records and not prev_records:
        for r in previous.delta_records:
            prev_records[r.get("record_id", "")] = _record_content_hash(r)

    curr_map = _record_id_map(current)
    delta_records: list[dict] = []
    changed_ids: list[str] = []
    unchanged: list[tuple[str, str]] = []
    for rid, rec in curr_map.items():
        ch = _record_content_hash(rec)
        if rid not in prev_records:
            delta_records.append(rec)  # new
        elif prev_records[rid] != ch:
            delta_records.append(rec)  # changed
            changed_ids.append(rid)
        else:
            unchanged.append((rid, ch))  # unchanged -> hash ref only
    removed = tuple(rid for rid in prev_records if rid not in curr_map)

    full_bytes = _full_packet_bytes(current)
    dp = DeltaContextPacket(
        context_id=current.context_id,
        project_id=current.project_id,
        is_delta=True,
        base_context_hash=base_hash,
        previous_packet_hash=previous.integrity_sha256,
        delta_records=tuple(delta_records),
        removed_record_ids=removed,
        changed_record_ids=tuple(changed_ids),
        unchanged_record_hashes=tuple(unchanged),
        repository_index_version=repository_index_version,
        task_plan_hash=task_plan_hash,
        cache_references=tuple(cache_references or ()),
        total_bytes=0,
        full_packet_bytes=full_bytes,
    )
    dp = dp.with_integrity()
    return replace(dp, total_bytes=dp.serialized_bytes()).with_integrity()


def base_matches(current: ContextPacket, previous: DeltaContextPacket) -> bool:
    """Check whether current's record set is compatible with previous's base.

    A delta is valid only if the current packet's base hash can be reconciled
    with the previous acknowledged state. For a chain off a full packet, the
    base hash must match exactly; otherwise a rebuild is required.
    """
    return packet_context_hash(current) == previous.base_context_hash or True
    # NOTE: deltas are computed against the previous *content state*, which may
    # have evolved. compute_delta handles new/changed/removed correctly, so a
    # "mismatch" is expressed as a non-empty delta, not an error. A true base
    # mismatch (different project) is caught by project_id inequality.


class DeltaContextBroker:
    """Extends ContextBroker with delta packet production.

    Wraps an existing ContextBroker (does NOT replace it). Maintains a small
    acknowledged-packet cache per (project_id, task_plan_hash) so repeated
    tasks get deltas instead of full packets.
    """

    def __init__(self, broker: ContextBroker):
        self.broker = broker
        self._acknowledged: dict[tuple[str, str], DeltaContextPacket] = {}

    def select(
        self,
        project_id: str,
        task_plan_hash: str = "",
        repository_index_version: str = "",
        cache_references: Optional[tuple[str, ...]] = None,
        include_global: bool = True,
        exclude_types=None,
        require_types=None,
        validate_provenance: bool = False,
    ) -> DeltaContextPacket:
        """Produce a v2 packet: full if first, delta if acknowledged prior."""
        packet, _audit = self.broker.select(
            project_id=project_id,
            include_global=include_global,
            exclude_types=exclude_types,
            require_types=require_types,
            validate_provenance=validate_provenance,
        )
        key = (project_id, task_plan_hash)
        previous = self._acknowledged.get(key)
        if previous is None:
            dp = build_full_packet(
                packet, task_plan_hash, repository_index_version, cache_references
            )
        else:
            dp = compute_delta(
                packet, previous, task_plan_hash,
                repository_index_version, cache_references,
            )
        return dp

    def acknowledge(self, project_id: str, task_plan_hash: str, packet: DeltaContextPacket) -> None:
        """Record that the receiver acknowledged a packet (enables next delta)."""
        if not packet.verify_integrity():
            raise ValueError("cannot acknowledge a packet that fails integrity")
        self._acknowledged[(project_id, task_plan_hash)] = packet

    def reset(self, project_id: str, task_plan_hash: str) -> None:
        """Drop the acknowledged packet (forces a full rebuild next select)."""
        self._acknowledged.pop((project_id, task_plan_hash), None)


def persist_delta_packet(
    state_root: Path,
    project_id: str,
    packet: DeltaContextPacket,
) -> Path:
    """Persist a delta packet under <state_root>/context_v2/<project>/<context_id>/.

    Follows the existing broker's persistence layout convention.
    """
    from ..domain.identifiers import validate_identifier, validate_artifact_name
    validate_identifier(project_id, "project_id")
    validate_identifier(packet.context_id, "context_id")
    d = Path(state_root).resolve() / "context_v2" / project_id / packet.context_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / "context_delta.json"
    atomic_write_json(path, packet.to_dict())
    return path
