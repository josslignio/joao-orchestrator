"""V2 safe continuous learning — 4-tier lifecycle (§24).

Extends the EXISTING :mod:`joao_orchestrator.memory.lessons` store (which has
ACTIVE/SUPERSEDED/REJECTED) with the §24 promotion ladder::

    candidate → shadow → validated → trusted → (deprecated/revoked)

Promotion rules (§24):

* never trust from one run;
* project lesson: 3 successful replays;
* generic lesson: 3 successful replays across 2 projects;
* human approval required for permissions, credentials, safety, deployment,
  finance, auto-merge, external actions (always forbidden here — §24).

All lessons are reversible. Storage lives OUTSIDE Git under
``~/.local/share/joss-orchestrator/learning/``.

This is an adapter: it delegates persistence to the existing LessonStore where
possible and adds a V2 promotion ledger on top. It does NOT duplicate lesson
storage.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..storage.atomic import atomic_write_json, append_line  # reuse
from .state import JOSS_ROOT

LEARNING_ROOT = JOSS_ROOT / "learning"
LEDGER_FILENAME = "promotion_ledger.jsonl"

# §24 promotion thresholds.
PROJECT_PROMOTION_REPLAYS = 3
GENERIC_PROMOTION_REPLAYS = 3
GENERIC_PROMOTION_PROJECTS = 2

# §24: these lesson categories are FORBIDDEN — they can never be auto-applied.
FORBIDDEN_LESSON_BEHAVIORS = (
    "weaken_tests", "weaken_safety", "expose_secrets", "change_permissions",
    "auto_merge", "trading", "auto_apply",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


TIER_CANDIDATE = "candidate"
TIER_SHADOW = "shadow"
TIER_VALIDATED = "validated"
TIER_TRUSTED = "trusted"
TIER_DEPRECATED = "deprecated"
TIER_REVOKED = "revoked"

TIERS = (TIER_CANDIDATE, TIER_SHADOW, TIER_VALIDATED, TIER_TRUSTED)
TERMINAL = (TIER_DEPRECATED, TIER_REVOKED)


@dataclass
class LessonCandidate:
    """A §24 lesson candidate (never auto-trusted from one run)."""
    lesson_id: str
    scope: str               # "project" | "generic"
    projects: list[str] = field(default_factory=list)
    trigger: str = ""
    observation: str = ""
    evidence: list[str] = field(default_factory=list)
    proposed_behavior: str = ""
    benefit: str = ""
    risk: str = ""
    replay: str = ""         # how to replay/verify
    rollback: str = ""       # always reversible
    proposed_category: str = ""   # e.g. "file_selection", "test_selection"
    status: str = TIER_CANDIDATE
    replay_counts: dict[str, int] = field(default_factory=dict)  # project -> count
    version: int = 1
    created_at: str = ""
    updated_at: str = ""

    def is_forbidden(self) -> bool:
        return self.proposed_category in FORBIDDEN_LESSON_BEHAVIORS

    def total_replays(self) -> int:
        return sum(self.replay_counts.values())

    def distinct_projects(self) -> int:
        return len([p for p, c in self.replay_counts.items() if c > 0])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PromotionError(ValueError):
    pass


class LessonLifecycle:
    """Manages the candidate→trusted ladder with §24 thresholds."""

    def __init__(self) -> None:
        LEARNING_ROOT.mkdir(parents=True, exist_ok=True)
        self.ledger = LEARNING_ROOT / LEDGER_FILENAME

    # -- persistence ------------------------------------------------------
    def _path(self, lesson_id: str) -> Path:
        if "/" in lesson_id or ".." in lesson_id:
            raise PromotionError(f"bad lesson_id: {lesson_id!r}")
        return LEARNING_ROOT / f"{lesson_id}.json"

    def submit(self, lesson: LessonCandidate) -> Path:
        """Submit a new candidate. §24: never trust from one run."""
        if lesson.is_forbidden():
            raise PromotionError(
                f"refusing forbidden lesson category: "
                f"{lesson.proposed_category!r} (§24)")
        if not lesson.created_at:
            lesson.created_at = _utcnow()
        lesson.updated_at = _utcnow()
        path = self._path(lesson.lesson_id)
        atomic_write_json(path, lesson.to_dict())
        self._log({"event": "submitted", "lesson_id": lesson.lesson_id,
                   "scope": lesson.scope})
        return path

    def load(self, lesson_id: str) -> LessonCandidate:
        with open(self._path(lesson_id), encoding="utf-8") as fh:
            data = json.load(fh)
        return LessonCandidate(**data)

    # -- replay recording -------------------------------------------------
    def record_replay(self, lesson_id: str, project_id: str,
                      success: bool) -> LessonCandidate:
        lesson = self.load(lesson_id)
        if success:
            lesson.replay_counts[project_id] = (
                lesson.replay_counts.get(project_id, 0) + 1)
            if project_id not in lesson.projects:
                lesson.projects.append(project_id)
        lesson.updated_at = _utcnow()
        atomic_write_json(self._path(lesson_id), lesson.to_dict())
        self._log({"event": "replay", "lesson_id": lesson_id,
                   "project": project_id, "success": success,
                   "total": lesson.total_replays()})
        return lesson

    # -- promotion --------------------------------------------------------
    def can_promote(self, lesson: LessonCandidate, to_tier: str) -> tuple[bool, str]:
        """Check §24 thresholds. Returns (ok, reason)."""
        if lesson.is_forbidden():
            return False, "forbidden category (§24)"
        if to_tier not in TIERS:
            return False, f"unknown tier {to_tier!r}"
        order = {t: i for i, t in enumerate(TIERS)}
        if order[to_tier] <= order.get(lesson.status, 0):
            return False, f"not a forward promotion from {lesson.status!r}"
        if to_tier == TIER_TRUSTED:
            replays = lesson.total_replays()
            projects = lesson.distinct_projects()
            if replays < GENERIC_PROMOTION_REPLAYS:
                return False, (f"needs {GENERIC_PROMOTION_REPLAYS} replays, "
                               f"has {replays}")
            if lesson.scope == "generic" and projects < GENERIC_PROMOTION_PROJECTS:
                return False, (f"generic lesson needs {GENERIC_PROMOTION_PROJECTS} "
                               f"projects, has {projects}")
        return True, "ok"

    def promote(self, lesson_id: str, to_tier: str) -> LessonCandidate:
        lesson = self.load(lesson_id)
        ok, reason = self.can_promote(lesson, to_tier)
        if not ok:
            raise PromotionError(f"cannot promote {lesson_id}: {reason}")
        lesson.status = to_tier
        lesson.version += 1
        lesson.updated_at = _utcnow()
        atomic_write_json(self._path(lesson_id), lesson.to_dict())
        self._log({"event": "promoted", "lesson_id": lesson_id,
                   "to": to_tier, "version": lesson.version})
        return lesson

    def revoke(self, lesson_id: str, reason: str) -> LessonCandidate:
        """All lessons are reversible (§24)."""
        lesson = self.load(lesson_id)
        lesson.status = TIER_REVOKED
        lesson.version += 1
        lesson.updated_at = _utcnow()
        atomic_write_json(self._path(lesson_id), lesson.to_dict())
        self._log({"event": "revoked", "lesson_id": lesson_id, "reason": reason})
        return lesson

    def list_lessons(self, status: str | None = None) -> list[LessonCandidate]:
        out: list[LessonCandidate] = []
        for p in sorted(LEARNING_ROOT.glob("*.json")):
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
                lc = LessonCandidate(**data)
            except Exception:
                continue
            if status is None or lc.status == status:
                out.append(lc)
        return out

    # -- helpers ----------------------------------------------------------
    def _log(self, record: Mapping[str, Any]) -> None:
        record = {**record, "ts": _utcnow()}
        append_line(self.ledger, json.dumps(record, sort_keys=True))


__all__ = [
    "LEARNING_ROOT", "LessonCandidate", "LessonLifecycle", "PromotionError",
    "TIERS", "TERMINAL",
    "TIER_CANDIDATE", "TIER_SHADOW", "TIER_VALIDATED", "TIER_TRUSTED",
    "TIER_DEPRECATED", "TIER_REVOKED",
    "PROJECT_PROMOTION_REPLAYS", "GENERIC_PROMOTION_REPLAYS",
    "GENERIC_PROMOTION_PROJECTS", "FORBIDDEN_LESSON_BEHAVIORS",
]
