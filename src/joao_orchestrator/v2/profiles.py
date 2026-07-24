"""Generic V2 project-profile schema and loader.

Concrete project defaults and review patterns belong in repository-level
``project_profiles/<project-id>/profile.json`` files.  This module never
selects a product, imports a project profile, or embeds product literals.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass
class ProjectProfileV2:
    """A generic, data-only project profile."""

    project_id: str
    repository_path: str
    runtime_root: str
    known_good_command: str = ""
    public_urls: list[str] = field(default_factory=list)
    scheduler_identifier: str = ""
    mode: str = "ACTIVE"
    frozen: bool = False
    acceptance_checks: list[str] = field(default_factory=list)
    known_incidents: list[str] = field(default_factory=list)
    forbidden_patterns: list[dict[str, str]] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = "2.0"
        return data


def load_profile(profile_path: str | Path, **overrides: Any) -> ProjectProfileV2:
    """Load one explicitly selected profile file and apply safe overrides.

    Selecting a path is deliberate: the generic package has no registry of
    concrete projects and therefore cannot acquire a product dependency.
    """
    path = Path(profile_path)
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"profile must be an object: {path}")
    data = dict(raw)
    data.update(overrides)
    allowed = set(ProjectProfileV2.__dataclass_fields__)
    extensions = dict(data.pop("extensions", {}))
    for key in list(data):
        if key not in allowed:
            extensions[key] = data.pop(key)
    data["extensions"] = extensions
    return ProjectProfileV2(**data)


def forbidden_patterns_from_profile(
    profile: ProjectProfileV2 | Mapping[str, Any],
) -> Sequence[tuple[re.Pattern, str]]:
    """Compile review patterns supplied by the explicitly loaded profile."""
    entries = (profile.forbidden_patterns if isinstance(profile, ProjectProfileV2)
               else profile.get("forbidden_patterns", []))
    compiled: list[tuple[re.Pattern, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("forbidden_patterns entries must be objects")
        pattern, label = entry.get("pattern"), entry.get("label")
        if not isinstance(pattern, str) or not isinstance(label, str):
            raise ValueError("forbidden pattern requires string pattern and label")
        compiled.append((re.compile(pattern, re.I), label))
    return tuple(compiled)


__all__ = ["ProjectProfileV2", "load_profile", "forbidden_patterns_from_profile"]
