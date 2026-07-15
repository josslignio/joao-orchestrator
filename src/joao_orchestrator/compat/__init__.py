"""Compatibility adapters implemented in the canonical package."""

from .v140 import (LegacyRestrictedExecutor, build_zcode_prompt,
                   compat_detect_forbidden_changes, compat_is_forbidden)

__all__ = [
    "LegacyRestrictedExecutor",
    "build_zcode_prompt",
    "compat_detect_forbidden_changes",
    "compat_is_forbidden",
]
