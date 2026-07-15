"""C4 Validated Lessons Memory — deterministic retrieval index.

Deterministic-first retrieval:
  1. project match
  2. task category
  3. path overlap
  4. failure fingerprint
  5. lexical overlap
  6. recency tie-break

Limits (enforced):
  - max 5 lessons retrieved
  - max 1500 characters total
  - no raw executable instructions

Lesson text is UNTRUSTED bounded context. It may advise but NEVER expand
permission. The retrieval result includes only advisory text + provenance; it
never carries executable code or capability grants.

Deterministic. No network. Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .lessons import Lesson, MAX_RETRIEVED_LESSONS, MAX_RETRIEVED_CHARS


# Patterns that look like executable instructions (never retrieved as-is).
# These are intentionally broad: a retrieved lesson is advisory data, and a
# false positive here only causes the lesson to be skipped (safe). The blocklist
# covers shell metacharacters, destructive commands, and prompt-injection
# markers that should never be re-interpreted as instructions downstream.
_EXEC_PATTERNS = (
    # Shell destructive / privileged commands.
    r"\brm\s+-rf?\b",
    r"\bsudo\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bdd\b",
    r"\bmkfs\b",
    r"\b>(?:\s|/?dev)",
    r":\(\)\s*\{",                       # fork bomb
    # Shell out / code execution sinks.
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\b__import__\b",
    r"\bsubprocess\b",
    r"\bos\.system\b",
    r"\bos\.popen\b",
    r"\bpopen\b",
    # Network fetch + execute (common injection payload shape).
    r"\bcurl\b",
    r"\bwget\b",
    r"\bnc\b",
    r"\biex\b",                           # PowerShell invoke-expression
    # Git / package destructive.
    r"--force\b",
    r"--force-with-lease\b",
    r"\bgit\s+push\s+--force\b",
    r"\bpip\s+install\b",
    # SQL destructive.
    r"\bDROP\s+TABLE\b",
    r"\bDELETE\s+FROM\b",
    r"\bTRUNCATE\b",
    # Prompt-injection instruction markers in advisory text.
    r"ignore (?:previous|all|the) (?:policy|policies|instructions?|rules?)",
    r"disregard (?:the |all )?(?:policy|safety|security)",
    r"you (?:are |must |now )(?:authorized|allowed|permitted|approve)",
)
_EXEC_RE = [re.compile(p, re.IGNORECASE) for p in _EXEC_PATTERNS]


def _looks_executable(text: str) -> bool:
    return any(rx.search(text) for rx in _EXEC_RE)


def _lesson_text_fields(lesson: Lesson) -> list[str]:
    """All free-text fields of a lesson that must be scanned.

    Retrieves every field that could carry adversarial text, not only
    ``root_cause``/``validated_fix``: tags, applicable paths and the task
    category are all attacker-influenceable if a model authored the lesson.
    """
    return [
        lesson.root_cause,
        lesson.validated_fix,
        lesson.task_category,
        " ".join(lesson.tags),
        " ".join(lesson.applicable_paths),
    ]


def _lesson_has_executable_text(lesson: Lesson) -> bool:
    """True if ANY text field of the lesson looks executable/injective."""
    return any(_looks_executable(field) for field in _lesson_text_fields(lesson))


def _paths_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    set_a = {p.rstrip("/") for p in a}
    set_b = {p.rstrip("/") for p in b}
    if set_a & set_b:
        return True
    for pa in set_a:
        for pb in set_b:
            if pa.startswith(pb + "/") or pb.startswith(pa + "/"):
                return True
    return False


def _lexical_overlap(query: str, text: str) -> float:
    """Deterministic lexical overlap score (Jaccard on word tokens)."""
    qa = set(re.findall(r"[a-z0-9]+", query.lower()))
    ta = set(re.findall(r"[a-z0-9]+", text.lower()))
    if not qa or not ta:
        return 0.0
    return len(qa & ta) / len(qa | ta)


@dataclass(frozen=True)
class LessonRetrievalQuery:
    """Query for lesson retrieval."""

    project_id: str
    task_category: str = ""
    task_paths: tuple[str, ...] = ()
    failure_fingerprint: str = ""
    query_text: str = ""


@dataclass(frozen=True)
class RetrievedLesson:
    """One retrieved lesson (advisory text + provenance only).

    ``text_role`` is always ``"untrusted_advisory_data"``: downstream prompt
    builders MUST treat ``root_cause``/``validated_fix``/``tags`` as fenced
    quoted context and never as instructions. This field exists so consumers
    can branch on it rather than on convention.
    """

    lesson_id: str
    project_id: str
    task_category: str
    root_cause: str
    validated_fix: str
    applicable_paths: tuple[str, ...]
    tags: tuple[str, ...]
    source_commit: str
    created_at: str
    score: float
    match_reasons: tuple[str, ...]
    text_role: str = "untrusted_advisory_data"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lesson_id": self.lesson_id,
            "project_id": self.project_id,
            "task_category": self.task_category,
            "root_cause": self.root_cause,
            "validated_fix": self.validated_fix,
            "applicable_paths": list(self.applicable_paths),
            "tags": list(self.tags),
            "source_commit": self.source_commit,
            "created_at": self.created_at,
            "score": self.score,
            "match_reasons": list(self.match_reasons),
            "text_role": self.text_role,
        }


@dataclass(frozen=True)
class RetrievalResult:
    """Bounded retrieval result."""

    lessons: tuple[RetrievedLesson, ...]
    total_chars: int
    truncated: bool
    executable_blocked: int
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lessons": [l.to_dict() for l in self.lessons],
            "count": len(self.lessons),
            "total_chars": self.total_chars,
            "truncated": self.truncated,
            "executable_blocked": self.executable_blocked,
        }


def _score_lesson(lesson: Lesson, query: LessonRetrievalQuery) -> tuple[float, list[str]]:
    """Score a lesson against a query. Returns (score, reasons).

    Higher score = more relevant. Deterministic: same inputs -> same score.
    """
    score = 0.0
    reasons: list[str] = []

    # 1. Project match (strongest signal).
    if lesson.project_id == query.project_id:
        score += 100.0
        reasons.append("project_match")
    elif query.project_id:
        score -= 50.0  # cross-project penalty

    # 2. Task category.
    if query.task_category and lesson.task_category == query.task_category:
        score += 30.0
        reasons.append("category_match")

    # 3. Path overlap.
    if query.task_paths and _paths_overlap(lesson.applicable_paths, query.task_paths):
        score += 25.0
        reasons.append("path_overlap")

    # 4. Failure fingerprint.
    if query.failure_fingerprint and lesson.failure_fingerprint == query.failure_fingerprint:
        score += 40.0
        reasons.append("fingerprint_match")
    elif query.failure_fingerprint and query.failure_fingerprint in lesson.failure_fingerprint:
        score += 15.0
        reasons.append("fingerprint_substring")

    # 5. Lexical overlap.
    text = f"{lesson.root_cause} {lesson.validated_fix}"
    lex = _lexical_overlap(query.query_text, text)
    if lex > 0:
        score += lex * 20.0
        reasons.append(f"lexical_overlap({lex:.2f})")

    return score, reasons


def retrieve_lessons(
    store_lessons: Iterable[Lesson],
    query: LessonRetrievalQuery,
    max_lessons: int = MAX_RETRIEVED_LESSONS,
    max_chars: int = MAX_RETRIEVED_CHARS,
) -> RetrievalResult:
    """Retrieve bounded relevant lessons deterministically.

    Enforces:
      - max_lessons (default 5)
      - max_chars total (default 1500)
      - no executable instructions (blocked, counted)
      - only ACTIVE lessons
      - only same-project (cross-project leakage impossible: other-project
        lessons score below 0 and are never retrieved ahead of same-project)
    """
    scored: list[tuple[float, list[str], Lesson]] = []
    exec_blocked = 0
    for lesson in store_lessons:
        if lesson.status != "ACTIVE":
            continue
        # HARD project filter: cross-project lessons are NEVER retrieved.
        # This prevents leakage even if fingerprint/path/text matches would
        # otherwise push the score positive.
        if lesson.project_id != query.project_id:
            continue
        # Block lessons whose text contains executable/injective instructions.
        # Scans ALL free-text fields (root_cause, validated_fix, tags, paths,
        # category) not just two, so adversarial text cannot slip in via a
        # field the narrow blocklist ignored.
        if _lesson_has_executable_text(lesson):
            exec_blocked += 1
            continue
        score, reasons = _score_lesson(lesson, query)
        scored.append((score, reasons, lesson))

    # Sort by score descending, then by created_at (recency tie-break), then id.
    scored.sort(key=lambda x: (-x[0], x[2].created_at, x[2].lesson_id))

    selected: list[RetrievedLesson] = []
    total_chars = 0
    truncated = False
    for score, reasons, lesson in scored:
        if len(selected) >= max_lessons:
            truncated = True
            break
        # Only positive-scoring lessons are relevant.
        if score <= 0:
            break
        chars = lesson.content_chars
        if total_chars + chars > max_chars:
            truncated = True
            break
        selected.append(RetrievedLesson(
            lesson_id=lesson.lesson_id,
            project_id=lesson.project_id,
            task_category=lesson.task_category,
            root_cause=lesson.root_cause,
            validated_fix=lesson.validated_fix,
            applicable_paths=lesson.applicable_paths,
            tags=lesson.tags,
            source_commit=lesson.source_commit,
            created_at=lesson.created_at,
            score=score,
            match_reasons=tuple(reasons),
        ))
        total_chars += chars

    return RetrievalResult(
        lessons=tuple(selected),
        total_chars=total_chars,
        truncated=truncated,
        executable_blocked=exec_blocked,
    )
