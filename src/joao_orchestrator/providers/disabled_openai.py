"""DisabledOpenAIProvider — placeholder for a future OpenAI adapter.

Future scope: Agents SDK, handoffs, sessions, tracing, tool guardrails.
V1.4.1: disabled. Raises DisabledProviderError with a structured explanation.
"""

from __future__ import annotations

from .base import DisabledProvider


class DisabledOpenAIProvider(DisabledProvider):
    name = "openai"
    supported_roles = ("planner", "reviewer", "coder")
    network_required = True
    what_is_missing = (
        "OpenAI provider adapter (Agents SDK, handoffs, sessions, tracing, "
        "tool guardrails) is not implemented. Requires API credentials and "
        "network access, both disabled in V1.4.1."
    )
