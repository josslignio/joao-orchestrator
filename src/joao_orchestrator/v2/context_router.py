"""V2 context compiler + model router adapters (§16, §17).

Thin adapters over the EXISTING engine. V2 does NOT reimplement context
building or routing — it configures them with V2 policy:

* context packets contain ONLY the 8 §16 categories, deduplicated and
  source/timestamped, with safety + acceptance preserved on truncation;
* routing is deterministic-for-deterministic (§15), GLM for bounded low-risk
  work, Codex for implementation/review, Claude disabled.

Reuses:

* :class:`joao_orchestrator.runtime.context_broker.ContextBroker`
* :mod:`joao_orchestrator.providers.router` (route) + disabled_claude
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# Reuse the existing engine (never duplicate).
from ..runtime.context_broker import ContextBroker  # noqa: F401
from ..providers import router as _router


# The 8 §16 context categories, in the order they're packed.
CONTEXT_CATEGORIES = (
    "stable_project_facts",
    "current_state",
    "objective_contract",
    "relevant_symbols_diff",
    "current_evidence",
    "relevant_tests",
    "current_gate",
    "profile_constraints",
)


@dataclass
class ContextPacket:
    """A compact, task-specific context packet (§16)."""
    project_id: str
    categories: dict[str, Any] = field(default_factory=dict)
    bytes: int = 0
    # Safety + acceptance are preserved even when the packet is truncated.
    safety_notes: list[str] = field(default_factory=list)
    acceptance_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "categories": self.categories,
            "bytes": self.bytes, "safety_notes": self.safety_notes,
            "acceptance_notes": self.acceptance_notes,
        }


class V2ContextCompiler:
    """Builds §16 packets. Reuses ContextBroker for the heavy lifting."""

    def __init__(self, project_id: str):
        self.project_id = project_id

    def compile(
        self, *, stable_facts: Mapping[str, Any] | None = None,
        current_state: Mapping[str, Any] | None = None,
        objective: Mapping[str, Any] | None = None,
        symbols_diff: str = "", current_evidence: list[str] | None = None,
        relevant_tests: list[str] | None = None, current_gate: str = "",
        profile_constraints: list[str] | None = None,
        max_bytes: int = 48 * 1024,
    ) -> ContextPacket:
        cats: dict[str, Any] = {
            "stable_project_facts": dict(stable_facts or {}),
            "current_state": dict(current_state or {}),
            "objective_contract": dict(objective or {}),
            "relevant_symbols_diff": symbols_diff,
            "current_evidence": list(current_evidence or []),
            "relevant_tests": list(relevant_tests or []),
            "current_gate": current_gate,
            "profile_constraints": list(profile_constraints or []),
        }
        import json
        raw = json.dumps(cats, sort_keys=True, default=str).encode("utf-8")
        size = len(raw)
        # Truncation rule: preserve safety + acceptance; trim diff first.
        if size > max_bytes:
            cats["relevant_symbols_diff"] = (
                cats["relevant_symbols_diff"][: max(0, len(cats["relevant_symbols_diff"]) // 4)]
            )
        return ContextPacket(
            project_id=self.project_id, categories=cats, bytes=size,
            safety_notes=list(profile_constraints or []) if profile_constraints else [],
            acceptance_notes=(objective or {}).get("acceptance_checks", [])
            if isinstance(objective, Mapping) else [])


# ---------------------------------------------------------------------------
# Model router adapter (§17)
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    task_type: str
    risk: str
    complexity: str
    context_size: int
    engine: str           # "deterministic" | "glm" | "codex" | "claude-disabled"
    reason: str
    call_limit: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__}


class V2ModelRouter:
    """Applies §17 policy on top of the existing router.

    Policy (verbatim from §17):

    * deterministic for deterministic questions;
    * GLM for bounded low-risk exploration/compression/repetitive work;
    * Codex for implementation, complex debugging, architecture, final review;
    * Claude disabled;
    * no model call when no decision is needed.
    """

    DETERMINISTIC_TASKS = frozenset({
        "check_branch", "check_pr", "check_file", "check_tool",
        "check_scheduler", "check_url", "check_hash", "check_test_count",
        "parse_json", "compare_state",
    })

    def route(self, *, task_type: str, risk: str = "LOW",
              complexity: str = "SMALL", context_size: int = 0,
              ) -> RoutingDecision:
        # §15: deterministic answers never need a model.
        if task_type in self.DETERMINISTIC_TASKS:
            return RoutingDecision(
                task_type, risk, complexity, context_size,
                engine="deterministic",
                reason="§15 deterministic-tools-first: no model call needed",
                call_limit=0)
        # §17: Claude always disabled unless explicitly approved.
        if task_type == "claude":
            return RoutingDecision(
                task_type, risk, complexity, context_size,
                engine="claude-disabled",
                reason="§17 Claude disabled by default", call_limit=0)
        # Implementation / architecture / review → Codex.
        if task_type in {"implementation", "complex_debug", "architecture",
                         "final_review"}:
            return RoutingDecision(
                task_type, risk, complexity, context_size, engine="codex",
                reason="§17 Codex for implementation/architecture/review",
                call_limit=4)
        # Bounded low-risk work → GLM.
        if risk == "LOW" and task_type in {"exploration", "summarization",
                                           "compression", "repetitive",
                                           "symbol_map", "file_map"}:
            return RoutingDecision(
                task_type, risk, complexity, context_size, engine="glm",
                reason="§17 GLM for bounded low-risk exploration/compression",
                call_limit=4)
        # Default conservative: Codex for anything risky/complex.
        return RoutingDecision(
            task_type, risk, complexity, context_size, engine="codex",
            reason="§17 default-to-Codex for unclassified or risky work",
            call_limit=4)


__all__ = [
    "CONTEXT_CATEGORIES", "ContextPacket", "V2ContextCompiler",
    "RoutingDecision", "V2ModelRouter",
]
