"""DisabledHermesProvider — placeholder for a future Hermes adapter.

Future scope: research, memory, skill worker. Per the design, Hermes has NO
authority over policy or approvals.
V1.4.1: disabled.
"""

from __future__ import annotations

from .base import DisabledProvider


class DisabledHermesProvider(DisabledProvider):
    name = "hermes"
    supported_roles = ("researcher", "skill_worker")
    network_required = True
    what_is_missing = (
        "Hermes provider adapter (research, memory, skill worker) is not "
        "implemented. Hermes will have NO authority over policy or approvals. "
        "Requires credentials and network access, disabled in V1.4.1."
    )
