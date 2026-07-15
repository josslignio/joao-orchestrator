"""Deterministic bounded context broker (V1 — JOSS-452).

Selects only relevant memory records from the JOSS-451 memory layer and
produces a bounded ``context_packet`` for downstream consumers (provider
dispatch, convergence review, autopilot pre-check, human review).

Outputs::

    <state_root>/context/
        <project_id>/
            <context_id>/
                context_packet.json          # machine-readable structured context
                context_packet.md             # human-readable markdown summary
                context_selection_audit.json  # full audit trail of selection decisions

Design invariants:

* Deterministic: same inputs + same memory state → same packet (records sorted
  by created_at, record_id tiebreaker).
* Bounded: strict limits on record count, byte budget, per-record truncation.
* Exclusive: inactive, superseded, and other-project records are never included.
* Mandatory context: required record types (``request``, ``criteria``) must fit;
  if they exceed the byte budget, the broker raises ``ContextOverflowError``.
* Provenance-preserving: truncated records carry ``source_sha256`` (the original
  record hash) and ``content_truncated=True``; the original ``sha256`` is never
  claimed to match truncated content. The broker never mutates a memory record's
  ``sha256`` field.
* Audit-trail cross-check: when ``validate_provenance=True``, the broker calls
  ``MemoryStore.validate()`` to confirm every snapshot record also appears in
  the append-only JSONL audit log.
* Append-only audit: every selection decision is recorded in the audit artifact.
* Atomic writes: all three artifacts are written via write-tmp + fsync +
  os.replace. No partial files survive a crash.
* No network, no subprocess, no secrets, no shell=True.
* Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..domain.identifiers import validate_identifier, validate_artifact_name
from ..storage.atomic import atomic_write_json, atomic_write_text
from ..storage.memory import (
    MemoryStore,
    MemoryError,
    ProvenanceError,
    memory_root,
    project_memory_dir,
    global_memory_dir,
)

CONTEXT_BROKER_SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 50
DEFAULT_MAX_BYTES = 131_072          # 128 KiB
DEFAULT_MAX_BYTES_PER_RECORD = 8192  # 8 KiB
# Reserve headroom for the truncation suffix so the truncated content + suffix
# never exceeds the per-record limit.
_TRUNCATION_SUFFIX_OVERHEAD = 32

# Record types that are considered mandatory — must fit within the budget.
MANDATORY_TYPES: Tuple[str, ...] = ("request", "criteria", "project_brief")


class ContextOverflowError(ValueError):
    """Raised when mandatory context exceeds the byte budget."""
    pass


class ContextBrokerError(ValueError):
    """Base class for context broker usage errors."""
    pass


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class SelectionDecision:
    """One record's selection outcome."""
    record_id: str
    record_type: str
    title: str
    selected: bool
    reason: str           # human-readable reason (include/exclude)
    byte_size: int = 0    # original content size in bytes
    selected_bytes: int = 0  # bytes after truncation (0 if not selected)
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ContextPacket:
    """The bounded context delivered to consumers."""
    context_id: str
    project_id: str
    schema_version: int = CONTEXT_BROKER_SCHEMA_VERSION
    records: List[dict] = field(default_factory=list)
    total_bytes: int = 0
    record_count: int = 0
    max_bytes: int = DEFAULT_MAX_BYTES
    max_records: int = DEFAULT_MAX_RECORDS
    excluded_types: List[str] = field(default_factory=list)
    truncated_count: int = 0
    mandatory_types_present: List[str] = field(default_factory=list)
    mandatory_types_missing: List[str] = field(default_factory=list)
    created_at: str = ""
    sources: List[str] = field(default_factory=list)  # "project", "global"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ContextPacket":
        return cls(
            context_id=d["context_id"],
            project_id=d.get("project_id", ""),
            schema_version=int(d.get("schema_version", CONTEXT_BROKER_SCHEMA_VERSION)),
            records=d.get("records", []),
            total_bytes=int(d.get("total_bytes", 0)),
            record_count=int(d.get("record_count", 0)),
            max_bytes=int(d.get("max_bytes", DEFAULT_MAX_BYTES)),
            max_records=int(d.get("max_records", DEFAULT_MAX_RECORDS)),
            excluded_types=d.get("excluded_types", []),
            truncated_count=int(d.get("truncated_count", 0)),
            mandatory_types_present=d.get("mandatory_types_present", []),
            mandatory_types_missing=d.get("mandatory_types_missing", []),
            created_at=d.get("created_at", ""),
            sources=d.get("sources", []),
        )


@dataclass
class ContextSelectionAudit:
    """Full audit trail of a context selection pass."""
    context_id: str
    project_id: str
    schema_version: int = CONTEXT_BROKER_SCHEMA_VERSION
    decisions: List[dict] = field(default_factory=list)
    total_candidates: int = 0
    selected_count: int = 0
    excluded_count: int = 0
    overflow_count: int = 0
    byte_budget_used: int = 0
    byte_budget_limit: int = DEFAULT_MAX_BYTES
    record_limit_used: int = 0
    record_limit: int = DEFAULT_MAX_RECORDS
    mandatory_overflow: bool = False
    provenance_validated: bool = False
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"))


def _content_byte_size(record: dict) -> int:
    """Byte size of a record's content field (UTF-8 encoded)."""
    return len((record.get("content") or "").encode("utf-8"))


def _truncate_content(content: str, limit: int,
                      suffix: str = " …[+{excess} bytes]") -> str:
    """Truncate content to *limit* bytes (including suffix), appending a
    byte-count suffix.

    The suffix overhead is reserved so the returned string never exceeds
    *limit* bytes when encoded as UTF-8. If *limit* is smaller than the suffix
    overhead, an empty string is returned (fail safe).
    """
    if not content:
        return ""
    encoded = content.encode("utf-8")
    if len(encoded) <= limit:
        return content
    # Reserve space for the suffix so total ≤ limit.
    content_budget = max(0, limit - _TRUNCATION_SUFFIX_OVERHEAD)
    if content_budget == 0:
        return ""
    # Truncate to content_budget bytes, don't break a multi-byte char.
    truncated = encoded[:content_budget].decode("utf-8", errors="ignore")
    excess = len(encoded) - len(truncated.encode("utf-8"))
    return truncated.rstrip() + suffix.format(excess=excess)


def _build_markdown_packet(packet: ContextPacket) -> str:
    """Render a human-readable markdown summary of the context packet."""
    lines: List[str] = []
    lines.append(f"# Context packet — {packet.context_id}")
    lines.append("")
    lines.append(f"- **Project:** `{packet.project_id}`")
    lines.append(f"- **Records:** {packet.record_count} / {packet.max_records}")
    lines.append(f"- **Bytes:** {packet.total_bytes} / {packet.max_bytes}")
    lines.append(f"- **Truncated:** {packet.truncated_count}")
    lines.append(f"- **Sources:** {', '.join(packet.sources) or '(none)'}")
    if packet.mandatory_types_missing:
        lines.append(f"- **⚠ Missing mandatory types:** {', '.join(packet.mandatory_types_missing)}")
    lines.append(f"- **Generated:** {packet.created_at}")
    lines.append("")

    if packet.mandatory_types_present:
        lines.append("## Mandatory context")
        lines.append(f"Present: {', '.join(packet.mandatory_types_present)}")
        lines.append("")

    if packet.records:
        lines.append("## Selected records")
        lines.append("")
        for rec in packet.records:
            rtype = rec.get("record_type", "?")
            title = rec.get("title", "?")
            rid = rec.get("record_id", "?")
            content = rec.get("content", "")
            tags = rec.get("tags", [])
            truncated_flag = rec.get("content_truncated", False)
            lines.append(f"### [{rtype}] {title}")
            lines.append(f"`{rid}`")
            if tags:
                lines.append(f"Tags: {', '.join(tags)}")
            if truncated_flag:
                lines.append("_content truncated — see `source_sha256` for original hash_")
            # Truncate for display only (does not affect stored content).
            if len(content) > 2000:
                lines.append(content[:2000].rstrip() + " …[truncated in display]")
            else:
                lines.append(content)
            lines.append("")
    else:
        lines.append("_(no records selected)_")
        lines.append("")

    lines.append("---")
    lines.append("Deterministic bounded context. No secrets, no full diffs.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Context Broker
# --------------------------------------------------------------------------- #

class ContextBroker:
    """Deterministic, bounded context selection from the memory layer.

    Reads active records from project + global memory, applies mandatory-type
    and budget constraints, and produces three artifacts: the structured packet
    (JSON), a human-readable markdown summary, and a full audit trail.

    All selection logic is deterministic: records are sorted by
    ``created_at ASC, record_id ASC`` before budget application, so the same
    memory state always produces the same packet.
    """

    def __init__(
        self,
        state_root: Path,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_bytes_per_record: int = DEFAULT_MAX_BYTES_PER_RECORD,
        mandatory_types: Optional[Tuple[str, ...]] = None,
        now_fn=None,
    ):
        # Input validation — fail closed on invalid configuration.
        if not isinstance(max_records, int) or max_records <= 0:
            raise ContextBrokerError(
                f"max_records must be a positive integer, got: {max_records!r}")
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ContextBrokerError(
                f"max_bytes must be a positive integer, got: {max_bytes!r}")
        if not isinstance(max_bytes_per_record, int) or max_bytes_per_record <= 0:
            raise ContextBrokerError(
                f"max_bytes_per_record must be a positive integer, got: "
                f"{max_bytes_per_record!r}")
        # max_records must be >= number of mandatory types, else mandatory
        # records can never fit.
        mt = mandatory_types or MANDATORY_TYPES
        if max_records < len(mt):
            raise ContextBrokerError(
                f"max_records ({max_records}) is smaller than the number of "
                f"mandatory types ({len(mt)}); mandatory records could never fit")

        self.state_root = Path(state_root).expanduser().resolve()
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.max_bytes_per_record = max_bytes_per_record
        self.mandatory_types = mt
        self._now_fn = now_fn or _now_iso
        self.memory = MemoryStore(state_root)

    # -- path helpers ---------------------------------------------------- #

    def _context_dir(self, project_id: str, context_id: str) -> Path:
        validate_identifier(project_id, "project_id")
        validate_identifier(context_id, "context_id")
        return (memory_root(self.state_root) / "projects" / project_id
                / "context" / context_id)

    def _packet_json_path(self, project_id: str, context_id: str) -> Path:
        return self._context_dir(project_id, context_id) / "context_packet.json"

    def _packet_md_path(self, project_id: str, context_id: str) -> Path:
        return self._context_dir(project_id, context_id) / "context_packet.md"

    def _audit_path(self, project_id: str, context_id: str) -> Path:
        return self._context_dir(project_id, context_id) / "context_selection_audit.json"

    # -- context ID generation ------------------------------------------- #

    def new_context_id(self, project_id: str) -> str:
        """Generate a deterministic context ID (``ctx-<timestamp>-<project_short>``)."""
        ts = self._now_fn().replace("Z", "").replace("-", "").replace(":", "").replace("T", "-")
        short = project_id[:16] if len(project_id) > 16 else project_id
        return f"ctx-{ts}-{short}"

    # -- provenance cross-check ------------------------------------------ #

    def _validate_provenance(self, project_id: str, include_global: bool) -> bool:
        """Cross-check snapshot against the append-only JSONL audit log.

        Delegates to ``MemoryStore.validate()`` which verifies every snapshot
        record also appears in the audit log (and that hashes match). Raises
        ``ProvenanceError`` on any inconsistency.
        """
        # Always validate project scope.
        self.memory.validate(project_id)
        if include_global:
            self.memory.validate(None)
        return True

    # -- core selection logic -------------------------------------------- #

    def select(
        self,
        project_id: str,
        context_id: Optional[str] = None,
        include_global: bool = True,
        exclude_types: Optional[Set[str]] = None,
        require_types: Optional[Tuple[str, ...]] = None,
        validate_provenance: bool = False,
    ) -> Tuple[ContextPacket, ContextSelectionAudit]:
        """Select relevant memory records and build a bounded context packet.

        Args:
            project_id: The project to build context for.
            context_id: Optional explicit ID; generated if None.
            include_global: Whether to include global memory records.
            exclude_types: Record types to exclude (beyond inactive/superseded).
            require_types: Override mandatory types for this call.
            validate_provenance: If True, cross-check snapshot vs JSONL audit
                log before selection. Raises ``ProvenanceError`` on mismatch.

        Returns:
            (ContextPacket, ContextSelectionAudit) tuple.

        Raises:
            ContextOverflowError: If mandatory records exceed byte budget.
            ProvenanceError: Propagated from memory store reads or validation.
            ContextBrokerError: On invalid configuration.
        """
        pid = validate_identifier(project_id, "project_id")
        ctx_id = context_id or self.new_context_id(pid)
        validate_identifier(ctx_id, "context_id")
        ex_types = exclude_types or set()
        req_types = require_types or self.mandatory_types

        # Optional provenance cross-check: snapshot ↔ JSONL audit log.
        provenance_validated = False
        if validate_provenance:
            self._validate_provenance(pid, include_global)
            provenance_validated = True

        # 1. Gather all active records (project-scoped first, then global).
        decisions: List[SelectionDecision] = []
        sources: List[str] = []
        project_records = self.memory.list_active(pid)
        global_records = self.memory.list_active(None) if include_global else []

        # 2. Filter and sort all candidates deterministically.
        candidates: List[dict] = []
        for rec in project_records:
            candidates.append(rec)
            rec["_source"] = "project"
        for rec in global_records:
            # Exclude global records that belong to another project.
            rec_pid = rec.get("project_id", "")
            if rec_pid and rec_pid != pid:
                decisions.append(SelectionDecision(
                    record_id=rec["record_id"],
                    record_type=rec.get("record_type", ""),
                    title=rec.get("title", ""),
                    selected=False,
                    reason="wrong_project",
                    byte_size=_content_byte_size(rec),
                ))
                continue
            candidates.append(rec)
            rec["_source"] = "global"

        # Sort deterministically: created_at ASC, record_id ASC.
        candidates.sort(key=lambda r: (r.get("created_at", ""), r.get("record_id", "")))

        # 3. Two-pass selection: mandatory first, then optional.
        selected_records: List[dict] = []
        mandatory_bytes = 0

        optional_candidates: List[dict] = []

        for rec in candidates:
            rid = rec["record_id"]
            rtype = rec.get("record_type", "")
            title = rec.get("title", "")
            content = rec.get("content", "")
            raw_size = _content_byte_size(rec)

            # Exclude by type filter.
            if rtype in ex_types:
                decisions.append(SelectionDecision(
                    record_id=rid, record_type=rtype, title=title,
                    selected=False, reason="excluded_type", byte_size=raw_size))
                continue

            is_mandatory = rtype in req_types

            if is_mandatory:
                # Mandatory records: truncate to per-record limit, measure.
                per_record_limit = min(self.max_bytes_per_record, self.max_bytes)
                truncated_content = _truncate_content(content, per_record_limit)
                truncated_size = len(truncated_content.encode("utf-8"))
                truncated = len(truncated_content.encode("utf-8")) < raw_size

                selected_rec = self._build_selected_record(
                    rec, truncated_content, truncated)
                selected_records.append(selected_rec)
                mandatory_bytes += truncated_size
                if rec.get("_source") and rec["_source"] not in sources:
                    sources.append(rec["_source"])

                decisions.append(SelectionDecision(
                    record_id=rid, record_type=rtype, title=title,
                    selected=True, reason="mandatory",
                    byte_size=raw_size, selected_bytes=truncated_size,
                    truncated=truncated))
            else:
                optional_candidates.append(rec)

        # Check mandatory overflow BEFORE selecting optional.
        if mandatory_bytes > self.max_bytes:
            # Fail closed: mandatory context must fit.
            audit = self._build_audit(
                ctx_id, pid, decisions, mandatory_bytes,
                mandatory_overflow=True,
                sources=sources,
                provenance_validated=provenance_validated,
            )
            raise ContextOverflowError(
                f"Mandatory context ({mandatory_bytes} bytes) exceeds budget "
                f"({self.max_bytes} bytes). Reduce mandatory record content or "
                f"increase max_bytes.")

        # 4. Select optional records within remaining budget.
        remaining_bytes = self.max_bytes - mandatory_bytes
        remaining_records = self.max_records - len(selected_records)
        optional_bytes = 0

        for rec in optional_candidates:
            raw_size = _content_byte_size(rec)
            if remaining_records <= 0:
                decisions.append(SelectionDecision(
                    record_id=rec["record_id"],
                    record_type=rec.get("record_type", ""),
                    title=rec.get("title", ""),
                    selected=False, reason="record_limit",
                    byte_size=raw_size))
                continue

            per_record_limit = min(self.max_bytes_per_record, remaining_bytes)
            truncated_content = _truncate_content(
                rec.get("content", ""), per_record_limit)
            truncated_size = len(truncated_content.encode("utf-8"))
            truncated = truncated_size < raw_size

            if truncated_size > remaining_bytes or truncated_size == 0:
                decisions.append(SelectionDecision(
                    record_id=rec["record_id"],
                    record_type=rec.get("record_type", ""),
                    title=rec.get("title", ""),
                    selected=False, reason="byte_budget",
                    byte_size=raw_size))
                continue

            selected_rec = self._build_selected_record(
                rec, truncated_content, truncated)
            selected_records.append(selected_rec)
            remaining_bytes -= truncated_size
            optional_bytes += truncated_size
            remaining_records -= 1

            src = rec.get("_source", "")
            if src and src not in sources:
                sources.append(src)

            decisions.append(SelectionDecision(
                record_id=rec["record_id"],
                record_type=rec.get("record_type", ""),
                title=rec.get("title", ""),
                selected=True, reason="optional",
                byte_size=raw_size, selected_bytes=truncated_size,
                truncated=truncated))

        # 5. Determine mandatory type presence.
        present_types = {r.get("record_type", "") for r in selected_records}
        mandatory_present = sorted(present_types & set(req_types))
        mandatory_missing = sorted(set(req_types) - present_types)

        # 6. Build the packet.
        total_bytes = mandatory_bytes + optional_bytes
        truncated_total = sum(1 for d in decisions if d.truncated)

        packet = ContextPacket(
            context_id=ctx_id,
            project_id=pid,
            records=selected_records,
            total_bytes=total_bytes,
            record_count=len(selected_records),
            max_bytes=self.max_bytes,
            max_records=self.max_records,
            excluded_types=sorted(ex_types),
            truncated_count=truncated_total,
            mandatory_types_present=mandatory_present,
            mandatory_types_missing=mandatory_missing,
            created_at=self._now_fn(),
            sources=sources,
        )

        # 7. Build the audit.
        audit = self._build_audit(
            ctx_id, pid, decisions,
            total_bytes,
            mandatory_overflow=False,
            sources=sources,
            provenance_validated=provenance_validated,
        )

        return packet, audit

    def _build_selected_record(self, source_record: dict,
                                truncated_content: str,
                                truncated: bool) -> dict:
        """Build a packet record from a source memory record.

        Preserves the original ``sha256`` as ``source_sha256`` so the original
        record's provenance is traceable even when content is truncated. The
        ``content_truncated`` flag indicates whether ``content`` was shortened.
        The record's own ``sha256`` field is removed from the packet record to
        avoid implying it matches the (possibly truncated) content.
        """
        # Copy all fields except the internal _source marker and sha256
        # (which only matches the original, untruncated content).
        rec = {k: v for k, v in source_record.items()
               if k not in ("_source", "sha256")}
        rec["content"] = truncated_content
        rec["content_truncated"] = bool(truncated)
        rec["source_sha256"] = source_record.get("sha256", "")
        return rec

    def _build_audit(
        self,
        context_id: str,
        project_id: str,
        decisions: List[SelectionDecision],
        byte_budget_used: int,
        mandatory_overflow: bool,
        sources: List[str],
        provenance_validated: bool,
    ) -> ContextSelectionAudit:
        selected = [d for d in decisions if d.selected]
        excluded = [d for d in decisions if not d.selected]
        overflow = [d for d in decisions if d.reason == "byte_budget"]
        return ContextSelectionAudit(
            context_id=context_id,
            project_id=project_id,
            decisions=[d.to_dict() for d in decisions],
            total_candidates=len(decisions),
            selected_count=len(selected),
            excluded_count=len(excluded),
            overflow_count=len(overflow),
            byte_budget_used=byte_budget_used,
            byte_budget_limit=self.max_bytes,
            record_limit_used=len(selected),
            record_limit=self.max_records,
            mandatory_overflow=mandatory_overflow,
            provenance_validated=provenance_validated,
            created_at=self._now_fn(),
        )

    # -- persistence ------------------------------------------------------ #

    def persist(
        self,
        packet: ContextPacket,
        audit: ContextSelectionAudit,
    ) -> Dict[str, str]:
        """Write all three artifacts atomically.

        Each artifact is written via write-tmp + fsync + os.replace so a crash
        never leaves a partial file. Returns a dict mapping artifact name →
        absolute path.
        """
        ctx_dir = self._context_dir(packet.project_id, packet.context_id)
        ctx_dir.mkdir(parents=True, exist_ok=True)

        packet_path = self._packet_json_path(packet.project_id, packet.context_id)
        md_path = self._packet_md_path(packet.project_id, packet.context_id)
        audit_path = self._audit_path(packet.project_id, packet.context_id)

        # All three writes are atomic (write-tmp + fsync + os.replace).
        atomic_write_json(packet_path, packet.to_dict())
        atomic_write_text(md_path, _build_markdown_packet(packet))
        atomic_write_json(audit_path, audit.to_dict())

        return {
            "context_packet.json": str(packet_path),
            "context_packet.md": str(md_path),
            "context_selection_audit.json": str(audit_path),
        }

    # -- convenience entry point ------------------------------------------ #

    def build_context(
        self,
        project_id: str,
        context_id: Optional[str] = None,
        include_global: bool = True,
        exclude_types: Optional[Set[str]] = None,
        require_types: Optional[Tuple[str, ...]] = None,
        validate_provenance: bool = False,
    ) -> Tuple[ContextPacket, ContextSelectionAudit, Dict[str, str]]:
        """Select, build, and persist a context packet in one call.

        Returns (packet, audit, artifact_paths).
        """
        packet, audit = self.select(
            project_id, context_id, include_global, exclude_types,
            require_types, validate_provenance=validate_provenance)
        paths = self.persist(packet, audit)
        return packet, audit, paths

    # -- read-only queries ------------------------------------------------ #

    def load_packet(self, project_id: str, context_id: str) -> Optional[ContextPacket]:
        """Load a previously persisted context packet."""
        path = self._packet_json_path(project_id, context_id)
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return ContextPacket.from_dict(data)

    def load_audit(self, project_id: str, context_id: str) -> Optional[ContextSelectionAudit]:
        """Load a previously persisted audit."""
        path = self._audit_path(project_id, context_id)
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return ContextSelectionAudit(**data)

    def list_contexts(self, project_id: str) -> List[str]:
        """List context IDs for a project."""
        base = memory_root(self.state_root) / "projects" / project_id / "context"
        if not base.is_dir():
            return []
        return sorted(d.name for d in base.iterdir() if d.is_dir())
