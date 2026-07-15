"""Bounded automatic clarification bridge (V1 — JOSS-453 + JOSS-454).

Provides a deterministic, non-interactive clarification loop between the
ZCode/GLM implementation agent and the Codex CLI answerer:

    ZCode/GLM task
      → strict structured question  (QuestionRequest)
      → bounded context packet from JOSS-452
      → non-interactive Codex CLI call
      → strict structured answer     (ClarificationAnswer)
      → deterministic answer validation
      → persisted artifacts + hashes
      → original task resumes automatically

Design invariants:

* Bounded: at most 2 questions per task; a 3rd is rejected. Repeated
  identical questions are rejected.
* Strict schema: both QuestionRequest and ClarificationAnswer are validated
  against a fixed schema. Invalid JSON, unknown question ids, unknown options,
  low confidence, scope expansion, and policy expansion are all rejected.
* No free-running chat: Codex receives only the bounded context packet path
  and the exact question. No raw chat history is ever sent.
* Token stripping: provider/API token environment variables are stripped
  before any subprocess call. No credentials are persisted.
* No provider retry: a failed or rejected answer fails closed; the caller
  must escalate to human review.
* Codex is advisory only: it can never approve work or promote risk.
* Persistence: every request, response, hash, and decision is persisted as
  atomic JSON artifacts + an append-only JSONL audit log outside managed
  repositories.
* Standard library only. No network fallback, no paid API fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..domain.identifiers import validate_identifier, validate_artifact_name
from ..storage.atomic import atomic_write_json, atomic_write_text, append_line
from ..storage.memory import memory_root
from .context_broker import ContextBroker, ContextPacket

CLARIFICATION_SCHEMA_VERSION = 1
MAX_QUESTIONS_PER_TASK = 2
DEFAULT_MIN_CONFIDENCE = 0.7
DEFAULT_CODEX_TIMEOUT = 120

# Token patterns to strip from child environments (mirrors convergence.py).
_PROVIDER_TOKEN_PATTERNS = [
    re.compile(r"(?i)ZAI_"),
    re.compile(r"(?i)ZHIPU_"),
    re.compile(r"(?i)OPENAI_API_KEY"),
    re.compile(r"(?i)OPENAI_"),
    re.compile(r"(?i)ANTHROPIC_API_KEY"),
    re.compile(r"(?i)ANTHROPIC_"),
    re.compile(r"(?i)CLAUDE_.*KEY"),
    re.compile(r"(?i)CLAUDE_.*TOKEN"),
    re.compile(r"(?i)GITHUB_TOKEN"),
    re.compile(r"(?i)GH_TOKEN"),
    re.compile(r"(?i)GH_PAT"),
    re.compile(r"(?i)GITLAB_TOKEN"),
]


class ClarificationError(ValueError):
    """Base class for clarification bridge errors."""
    pass


class QuestionLimitExceeded(ClarificationError):
    """Raised when a task exceeds the maximum question count."""
    pass


class AnswerValidationError(ClarificationError):
    """Raised when a Codex answer fails deterministic validation."""
    pass


class ProviderUnavailable(ClarificationError):
    """Raised when the Codex CLI is not available."""
    pass


# --------------------------------------------------------------------------- #
# Hashing helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return (datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(obj: dict) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return _sha256_bytes(canonical)


def _strip_tokens(env: Dict[str, str]) -> Dict[str, str]:
    """Remove provider and GitHub token variables from env."""
    keys_to_strip = set()
    for k in env:
        for pat in _PROVIDER_TOKEN_PATTERNS:
            if pat.match(k):
                keys_to_strip.add(k)
                break
    return {k: v for k, v in env.items() if k not in keys_to_strip}


# --------------------------------------------------------------------------- #
# Question request schema (JOSS-453)
# --------------------------------------------------------------------------- #

_VALID_DECISION_TYPES = {
    "choice", "boolean", "estimate", "path_selection", "scope_confirmation",
}

_VALID_ANSWER_SCOPES = {
    "single_option", "boolean", "bounded_string", "bounded_list",
}


@dataclass
class QuestionRequest:
    """A strict structured clarification question."""
    schema_version: int = CLARIFICATION_SCHEMA_VERSION
    task_id: str = ""
    project_id: str = ""
    question_id: str = ""
    question: str = ""
    decision_type: str = "choice"
    options: List[str] = field(default_factory=list)
    allowed_answer_scope: str = "single_option"
    forbidden_scope_expansion: List[str] = field(default_factory=list)
    context_packet_path: str = ""
    context_packet_sha256: str = ""
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> None:
        """Validate the question request against the schema. Fail closed."""
        if self.schema_version != CLARIFICATION_SCHEMA_VERSION:
            raise ClarificationError(
                f"unsupported schema_version: {self.schema_version}")
        if not self.task_id:
            raise ClarificationError("task_id is required")
        if not self.project_id:
            raise ClarificationError("project_id is required")
        if not self.question_id:
            raise ClarificationError("question_id is required")
        if not self.question or not isinstance(self.question, str):
            raise ClarificationError("question is required and must be a string")
        if self.decision_type not in _VALID_DECISION_TYPES:
            raise ClarificationError(
                f"invalid decision_type: {self.decision_type!r}")
        if self.allowed_answer_scope not in _VALID_ANSWER_SCOPES:
            raise ClarificationError(
                f"invalid allowed_answer_scope: {self.allowed_answer_scope!r}")
        if self.decision_type in ("choice", "path_selection") and not self.options:
            raise ClarificationError(
                f"decision_type {self.decision_type!r} requires options")
        if not isinstance(self.min_confidence, (int, float)):
            raise ClarificationError("min_confidence must be numeric")
        if not (0.0 <= float(self.min_confidence) <= 1.0):
            raise ClarificationError(
                f"min_confidence must be in [0,1], got {self.min_confidence}")
        if not self.context_packet_path:
            raise ClarificationError("context_packet_path is required")
        if not self.context_packet_sha256:
            raise ClarificationError("context_packet_sha256 is required")
        if not self.created_at:
            raise ClarificationError("created_at is required")

    @classmethod
    def from_dict(cls, d: dict) -> "QuestionRequest":
        # Fail closed on unknown fields to enforce schema strictness.
        known = {"schema_version", "task_id", "project_id", "question_id",
                 "question", "decision_type", "options",
                 "allowed_answer_scope", "forbidden_scope_expansion",
                 "context_packet_path", "context_packet_sha256",
                 "min_confidence", "created_at"}
        unknown = set(d.keys()) - known
        if unknown:
            raise ClarificationError(
                f"unknown question fields: {sorted(unknown)}")
        return cls(
            schema_version=int(d.get("schema_version", CLARIFICATION_SCHEMA_VERSION)),
            task_id=d.get("task_id", ""),
            project_id=d.get("project_id", ""),
            question_id=d.get("question_id", ""),
            question=d.get("question", ""),
            decision_type=d.get("decision_type", "choice"),
            options=d.get("options", []),
            allowed_answer_scope=d.get("allowed_answer_scope", "single_option"),
            forbidden_scope_expansion=d.get("forbidden_scope_expansion", []),
            context_packet_path=d.get("context_packet_path", ""),
            context_packet_sha256=d.get("context_packet_sha256", ""),
            min_confidence=float(d.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
            created_at=d.get("created_at", ""),
        )


# --------------------------------------------------------------------------- #
# Answer schema (JOSS-454)
# --------------------------------------------------------------------------- #

_VALID_SAFETY_FLAGS = {
    "none", "low_risk", "needs_human_review", "policy_concern",
    "scope_expansion_detected",
}


@dataclass
class ClarificationAnswer:
    """A strict structured answer from the Codex CLI."""
    schema_version: int = CLARIFICATION_SCHEMA_VERSION
    question_id: str = ""
    selected_option: str = ""
    answer: str = ""
    rationale: str = ""
    confidence: float = 0.0
    scope_confirmation: str = ""
    safety_flags: List[str] = field(default_factory=list)
    answered_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def validate(self, question: QuestionRequest) -> None:
        """Validate the answer against the question. Fail closed."""
        if self.schema_version != CLARIFICATION_SCHEMA_VERSION:
            raise AnswerValidationError(
                f"unsupported schema_version: {self.schema_version}")
        if self.question_id != question.question_id:
            raise AnswerValidationError(
                f"question_id mismatch: answer={self.question_id!r} "
                f"question={question.question_id!r}")
        # Option validation.
        if question.decision_type in ("choice", "path_selection"):
            if self.selected_option not in question.options:
                raise AnswerValidationError(
                    f"selected_option {self.selected_option!r} not in "
                    f"options {question.options}")
        elif question.decision_type == "boolean":
            if self.selected_option not in ("true", "false", "yes", "no"):
                raise AnswerValidationError(
                    f"boolean answer must be true/false/yes/no, got "
                    f"{self.selected_option!r}")
        # Confidence validation.
        if not isinstance(self.confidence, (int, float)):
            raise AnswerValidationError("confidence must be numeric")
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise AnswerValidationError(
                f"confidence must be in [0,1], got {self.confidence}")
        if float(self.confidence) < float(question.min_confidence):
            raise AnswerValidationError(
                f"confidence {self.confidence} below minimum "
                f"{question.min_confidence}")
        # Safety flags.
        for flag in self.safety_flags:
            if flag not in _VALID_SAFETY_FLAGS:
                raise AnswerValidationError(
                    f"invalid safety_flag: {flag!r}")
        if "scope_expansion_detected" in self.safety_flags:
            raise AnswerValidationError(
                "scope expansion detected — answer rejected")
        if "policy_concern" in self.safety_flags:
            raise AnswerValidationError(
                "policy concern raised — answer rejected")
        # Scope confirmation.
        if question.allowed_answer_scope == "single_option":
            if not self.selected_option:
                raise AnswerValidationError(
                    "single_option scope requires selected_option")
        elif question.allowed_answer_scope == "boolean":
            if not self.selected_option:
                raise AnswerValidationError(
                    "boolean scope requires selected_option")
        elif question.allowed_answer_scope == "bounded_string":
            if not self.answer:
                raise AnswerValidationError(
                    "bounded_string scope requires answer")
            if len(self.answer) > 1000:
                raise AnswerValidationError(
                    "bounded_string answer exceeds 1000 chars")
        # Enforce forbidden_scope_expansion: if the answer text or rationale
        # references a forbidden scope item, reject.
        if question.forbidden_scope_expansion:
            combined = (self.answer + " " + self.rationale).lower()
            for forbidden in question.forbidden_scope_expansion:
                if forbidden.lower() in combined:
                    raise AnswerValidationError(
                        f"answer references forbidden scope: {forbidden!r}")

    @classmethod
    def from_dict(cls, d: dict) -> "ClarificationAnswer":
        # Fail closed on unknown fields to enforce schema strictness.
        known = {"schema_version", "question_id", "selected_option", "answer",
                 "rationale", "confidence", "scope_confirmation",
                 "safety_flags", "answered_at"}
        unknown = set(d.keys()) - known
        if unknown:
            raise AnswerValidationError(
                f"unknown answer fields: {sorted(unknown)}")
        return cls(
            schema_version=int(d.get("schema_version", CLARIFICATION_SCHEMA_VERSION)),
            question_id=d.get("question_id", ""),
            selected_option=d.get("selected_option", ""),
            answer=d.get("answer", ""),
            rationale=d.get("rationale", ""),
            confidence=float(d.get("confidence", 0.0)),
            scope_confirmation=d.get("scope_confirmation", ""),
            safety_flags=d.get("safety_flags", []),
            answered_at=d.get("answered_at", ""),
        )


# --------------------------------------------------------------------------- #
# Clarification decision record (audit)
# --------------------------------------------------------------------------- #

@dataclass
class ClarificationDecision:
    """One clarification exchange: question + answer + decision."""
    task_id: str
    project_id: str
    question_id: str
    question_sha256: str = ""
    answer_sha256: str = ""
    context_packet_sha256: str = ""
    accepted: bool = False
    rejection_reason: str = ""
    question_number: int = 0
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Clarification broker
# --------------------------------------------------------------------------- #

class ClarificationBroker:
    """Bounded automatic clarification bridge between ZCode and Codex.

    Enforces the 2-question limit, schema validation, context-hash binding,
    token stripping, and artifact persistence. Codex is advisory only.
    """

    def __init__(
        self,
        state_root: Path,
        codex_executable: str = "codex",
        timeout: int = DEFAULT_CODEX_TIMEOUT,
        max_questions: int = MAX_QUESTIONS_PER_TASK,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        now_fn=None,
    ):
        self.state_root = Path(state_root).expanduser().resolve()
        self.codex_executable = codex_executable
        self.timeout = timeout
        self.max_questions = max_questions
        self.min_confidence = min_confidence
        self._now_fn = now_fn or _now_iso
        self.context_broker = ContextBroker(state_root)

    # -- path helpers ---------------------------------------------------- #

    def _clarification_dir(self, project_id: str, task_id: str) -> Path:
        validate_identifier(project_id, "project_id")
        validate_identifier(task_id, "task_id")
        return (self.state_root / "tasks" / project_id / task_id
                / "clarifications")

    def _question_path(self, project_id: str, task_id: str,
                       question_id: str) -> Path:
        validate_artifact_name(question_id)
        return self._clarification_dir(project_id, task_id) / f"{question_id}_question.json"

    def _answer_path(self, project_id: str, task_id: str,
                     question_id: str) -> Path:
        validate_artifact_name(question_id)
        return self._clarification_dir(project_id, task_id) / f"{question_id}_answer.json"

    def _audit_path(self, project_id: str, task_id: str) -> Path:
        return self._clarification_dir(project_id, task_id) / "clarification_audit.jsonl"

    def _state_path(self, project_id: str, task_id: str) -> Path:
        return self._clarification_dir(project_id, task_id) / "clarification_state.json"

    # -- state tracking -------------------------------------------------- #

    def _load_state(self, project_id: str, task_id: str) -> dict:
        path = self._state_path(project_id, task_id)
        if not path.is_file():
            return {
                "task_id": task_id,
                "project_id": project_id,
                "questions_asked": [],
                "question_count": 0,
                "schema_version": CLARIFICATION_SCHEMA_VERSION,
            }
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save_state(self, project_id: str, task_id: str, state: dict) -> None:
        atomic_write_json(self._state_path(project_id, task_id), state)

    # -- context hash verification --------------------------------------- #

    def _verify_context_packet(self, context_packet_path: str,
                               expected_sha256: str) -> str:
        """Verify the context packet file matches the expected hash.

        Returns the absolute path on success. Raises on mismatch.
        """
        path = Path(context_packet_path)
        if not path.is_file():
            raise ClarificationError(
                f"context packet not found: {context_packet_path}")
        data = path.read_bytes()
        actual = _sha256_bytes(data)
        if actual != expected_sha256:
            raise ClarificationError(
                f"context packet hash mismatch: expected={expected_sha256} "
                f"actual={actual}")
        return str(path.resolve())

    # -- public API ------------------------------------------------------ #

    def ask(
        self,
        question: QuestionRequest,
        context_packet_path: str,
        context_packet_sha256: str,
        answerer_fn=None,
    ) -> Tuple[ClarificationAnswer, ClarificationDecision]:
        """Ask a clarification question and get a validated answer.

        Args:
            question: The structured question.
            context_packet_path: Path to the bounded context packet JSON.
            context_packet_sha256: Expected SHA-256 of the context packet.
            answerer_fn: Optional callable(prompt_str) -> str. If None,
                the real Codex CLI is invoked. Tests inject a fake.

        Returns:
            (ClarificationAnswer, ClarificationDecision) on success.

        Raises:
            QuestionLimitExceeded: If the task has already asked max_questions.
            ClarificationError: On repeated question, context mismatch, or
                schema violation.
            AnswerValidationError: If the answer fails validation.
            ProviderUnavailable: If the Codex CLI is not available.
        """
        question.validate()
        # Verify context packet hash BEFORE anything else.
        self._verify_context_packet(context_packet_path, context_packet_sha256)
        question.context_packet_path = context_packet_path
        question.context_packet_sha256 = context_packet_sha256
        if not question.created_at:
            question.created_at = self._now_fn()
        if not question.min_confidence:
            question.min_confidence = self.min_confidence

        # Load state and enforce limits.
        state = self._load_state(question.project_id, question.task_id)
        asked = state.get("questions_asked", [])
        count = state.get("question_count", 0)

        # Check question limit.
        if count >= self.max_questions:
            # Record the rejected attempt in the audit log.
            self._record_decision(
                question, state, accepted=False,
                rejection_reason=f"question limit exceeded ({count}/"
                                  f"{self.max_questions})",
                answer_sha256="",
            )
            raise QuestionLimitExceeded(
                f"task {question.task_id} already asked {count} questions "
                f"(max {self.max_questions})")

        # Check repeated question (by question_id or by question text hash).
        question_hash = _sha256_json({"question": question.question,
                                       "options": question.options})
        for prev in asked:
            if prev.get("question_id") == question.question_id:
                self._record_decision(
                    question, state, accepted=False,
                    rejection_reason=f"repeated question_id: "
                                      f"{question.question_id}",
                    answer_sha256="",
                )
                raise ClarificationError(
                    f"repeated question_id: {question.question_id}")
            if prev.get("question_hash") == question_hash:
                self._record_decision(
                    question, state, accepted=False,
                    rejection_reason="repeated identical question "
                                     "(same text + options)",
                    answer_sha256="",
                )
                raise ClarificationError(
                    "repeated identical question (same text + options)")

        # Persist the question artifact.
        q_path = self._question_path(question.project_id, question.task_id,
                                      question.question_id)
        atomic_write_json(q_path, question.to_dict())

        # Build the prompt sent to the answerer.
        prompt = self._build_prompt(question, context_packet_path)

        # Get the answer.
        if answerer_fn is not None:
            raw_answer = answerer_fn(prompt)
        else:
            try:
                raw_answer = self._call_codex(prompt)
            except ProviderUnavailable as exc:
                # Record the failure as a rejected decision before raising.
                self._record_decision(
                    question, state, accepted=False,
                    rejection_reason=f"provider unavailable: {exc}",
                    answer_sha256="",
                )
                raise

        # Parse and validate the answer.
        try:
            answer_data = json.loads(raw_answer)
        except json.JSONDecodeError as exc:
            decision = self._record_decision(
                question, state, accepted=False,
                rejection_reason=f"invalid JSON: {exc}",
                answer_sha256="",
            )
            raise AnswerValidationError(
                f"invalid JSON answer: {exc}") from exc

        answer = ClarificationAnswer.from_dict(answer_data)
        if not answer.answered_at:
            answer.answered_at = self._now_fn()

        answer_sha = _sha256_json(answer.to_dict())

        try:
            answer.validate(question)
        except AnswerValidationError as exc:
            self._record_decision(
                question, state, accepted=False,
                rejection_reason=str(exc),
                answer_sha256=answer_sha,
            )
            raise

        # Persist the answer artifact.
        a_path = self._answer_path(question.project_id, question.task_id,
                                    question.question_id)
        atomic_write_json(a_path, answer.to_dict())

        # Record the accepted decision.
        decision = self._record_decision(
            question, state, accepted=True,
            rejection_reason="",
            answer_sha256=answer_sha,
        )

        return answer, decision

    def _build_prompt(self, question: QuestionRequest,
                      context_packet_path: str) -> str:
        """Build the bounded prompt sent to the answerer.

        Contains ONLY the question and a reference to the context packet.
        No raw chat history, no full repository content.
        """
        return json.dumps({
            "instruction": (
                "Answer the following structured clarification question. "
                "Return ONLY a JSON object matching the answer schema. "
                "Do not expand scope. Do not promote risk."),
            "context_packet_path": context_packet_path,
            "question": question.to_dict(),
        }, sort_keys=True, separators=(",", ":"))

    def _call_codex(self, prompt: str) -> str:
        """Invoke the Codex CLI non-interactively.

        Strips provider token env vars. Raises ProviderUnavailable if the
        executable is missing.
        """
        exe = self.codex_executable
        # Resolve executable.
        from shutil import which
        resolved = which(exe) if "/" not in exe else (
            exe if Path(exe).is_file() else None)
        if resolved is None:
            raise ProviderUnavailable(
                f"codex executable not found: {exe}")

        env = _strip_tokens(dict(os.environ))
        try:
            result = subprocess.run(
                [resolved, "exec", "--json", "-s", "read-only",
                 "-C", str(self.state_root), prompt],
                capture_output=True, text=True, env=env,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderUnavailable(
                f"codex CLI timed out after {self.timeout}s") from exc
        if result.returncode != 0:
            raise ProviderUnavailable(
                f"codex CLI failed (rc={result.returncode}): "
                f"{result.stderr[:500]}")
        return result.stdout

    def _record_decision(
        self,
        question: QuestionRequest,
        state: dict,
        accepted: bool,
        rejection_reason: str,
        answer_sha256: str,
    ) -> ClarificationDecision:
        """Record a clarification decision in state + audit log."""
        q_sha = _sha256_json(question.to_dict())
        decision = ClarificationDecision(
            task_id=question.task_id,
            project_id=question.project_id,
            question_id=question.question_id,
            question_sha256=q_sha,
            answer_sha256=answer_sha256,
            context_packet_sha256=question.context_packet_sha256,
            accepted=accepted,
            rejection_reason=rejection_reason,
            question_number=state.get("question_count", 0) + 1,
            created_at=self._now_fn(),
        )

        # Update state.
        asked = state.setdefault("questions_asked", [])
        asked.append({
            "question_id": question.question_id,
            "question_hash": _sha256_json(
                {"question": question.question, "options": question.options}),
            "accepted": accepted,
            "decision_sha256": _sha256_json(decision.to_dict()),
        })
        state["question_count"] = state.get("question_count", 0) + 1
        self._save_state(question.project_id, question.task_id, state)

        # Append to audit log.
        audit_path = self._audit_path(question.project_id, question.task_id)
        append_line(audit_path, json.dumps(decision.to_dict(),
                                            sort_keys=True,
                                            ensure_ascii=False))
        return decision

    # -- read-only queries ------------------------------------------------ #

    def get_state(self, project_id: str, task_id: str) -> dict:
        return self._load_state(project_id, task_id)

    def list_decisions(self, project_id: str, task_id: str) -> List[dict]:
        audit = self._audit_path(project_id, task_id)
        if not audit.is_file():
            return []
        out = []
        with open(audit, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def can_ask(self, project_id: str, task_id: str) -> bool:
        """Return True if the task can still ask a question."""
        state = self._load_state(project_id, task_id)
        return state.get("question_count", 0) < self.max_questions
