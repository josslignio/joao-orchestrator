"""DisabledZAIProvider — placeholder for a future Z.AI / GLM adapter.

Future scope: OpenAI-compatible API, configurable base URL, general vs coding
endpoints.
V1.4.1: disabled.
"""

from __future__ import annotations

from .base import DisabledProvider


class DisabledZAIProvider(DisabledProvider):
    name = "zai"
    supported_roles = ("coder", "planner", "reviewer")
    network_required = True
    what_is_missing = (
        "Z.AI provider adapter (OpenAI-compatible API, configurable base URL, "
        "general vs coding endpoints) is not implemented. Requires API "
        "credentials and network access, both disabled in V1.4.1."
    )
