"""Validation helpers for identifiers used in filesystem and registry keys."""

from __future__ import annotations

import re

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_ARTIFACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_identifier(value: str, label: str = "identifier") -> str:
    """Return *value* when it is a single safe path component.

    Identifiers are persisted in SQLite and used as directory names.  They may
    not contain separators, traversal components, control characters, or an
    absolute path.
    """
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(
            f"invalid {label}: expected 1-128 characters matching "
            "[A-Za-z0-9][A-Za-z0-9._-]*"
        )
    if value in {".", ".."}:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def validate_artifact_name(value: str) -> str:
    """Return *value* when it is a safe, non-nested artifact filename."""
    if not isinstance(value, str) or not _SAFE_ARTIFACT.fullmatch(value):
        raise ValueError("invalid artifact name")
    if value in {".", ".."}:
        raise ValueError("invalid artifact name")
    return value
