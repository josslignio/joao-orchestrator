"""DisabledClaudeProvider — placeholder for a future Anthropic Claude adapter.

Future scope: Agent SDK, hooks, permission requests, subagents, MCP.
V1.4.1: disabled.
"""

from __future__ import annotations

from .base import DisabledProvider


class DisabledClaudeProvider(DisabledProvider):
    name = "claude"
    supported_roles = ("planner", "reviewer", "coder")
    network_required = True
    what_is_missing = (
        "Claude provider adapter (Agent SDK, hooks, permission requests, "
        "subagents, MCP) is not implemented. Requires API credentials and "
        "network access, both disabled in V1.4.1."
    )
