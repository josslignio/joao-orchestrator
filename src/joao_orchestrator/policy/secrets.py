"""Secret-handling policy: re-exports the redaction surface.

Kept as a distinct module so the policy layer has a single import surface for
secret concerns. V0.2 will add a VirtualCredentialProxy interface here.
"""

from __future__ import annotations

from ..observability.redaction import redact, redact_env, scrub_env_for_subprocess

__all__ = ["redact", "redact_env", "scrub_env_for_subprocess"]
