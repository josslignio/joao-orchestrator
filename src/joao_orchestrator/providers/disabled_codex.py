"""DisabledCodexProvider — legacy placeholder for unsupported Codex modes.

The local subscription CLI path lives in codex_subscription.py. Future Codex
app-server, stdio JSON-RPC, event streaming, and approval-request modes remain
disabled here.
"""

from __future__ import annotations

from .base import DisabledProvider


class DisabledCodexProvider(DisabledProvider):
    name = "codex"
    supported_roles = ("coder",)
    network_required = False  # local CLI, but still disabled
    what_is_missing = (
        "Codex app-server, stdio JSON-RPC, event streaming, and approval "
        "request modes are not implemented. Use the explicit "
        "codex-subscription engine for local CLI dispatch."
    )
