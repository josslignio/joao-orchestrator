"""joao_worker_host.protocol — the request/response contract between JOAO's
controller and the standalone `joao-worker-host` process.

Pure, fail-closed validation: malformed input is a controlled `ProtocolError`
the server converts into a BLOCK response, never an unhandled exception
reaching the socket handler.
"""
from __future__ import annotations

from typing import Any

REQUIRED_FIELDS = ("request_id", "run_id", "mission_id", "worker", "role", "model",
                   "workspace", "timeout", "mission")
VALID_ROLES = {"builder", "reviewer"}
# Controller-owned worker identities — the exact `provider` string each real
# adapter class carries (`GLMBuilder.provider == GLMReviewer.provider ==
# "zai-coding-plan"`; `ClaudeCodeBuilder.provider == ClaudeCLIReviewer.provider
# == CLAUDE_CLI_PROVIDER == "claude-cli"`). A request names WHICH registered
# worker to use; it never supplies or overrides that worker's own identity.
VALID_WORKERS = {"claude-cli", "zai-coding-plan"}
VALID_STAGES = {"plan", "build", "final"}


class ProtocolError(ValueError):
    pass


def _require_nonempty_str(payload: dict, field: str) -> None:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"request.{field} must be a non-empty string, got {value!r}")


def validate_request(payload: Any) -> dict[str, Any]:
    """Validate a decoded JSON request object. Returns `payload` unchanged
    once every check passes; raises `ProtocolError` on the first violation —
    never coerces, never fills in a silent default for a missing field."""
    if not isinstance(payload, dict):
        raise ProtocolError(f"request must be a JSON object, got {type(payload).__name__}")

    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise ProtocolError(f"request is missing required field(s) {missing}")

    for field in ("request_id", "run_id", "mission_id", "worker", "role", "model", "workspace", "mission"):
        _require_nonempty_str(payload, field)

    if payload["worker"] not in VALID_WORKERS:
        raise ProtocolError(f"request.worker must be one of {sorted(VALID_WORKERS)}, got {payload['worker']!r}")
    if payload["role"] not in VALID_ROLES:
        raise ProtocolError(f"request.role must be one of {sorted(VALID_ROLES)}, got {payload['role']!r}")

    timeout = payload.get("timeout")
    if type(timeout) is not int or timeout <= 0:
        raise ProtocolError(f"request.timeout must be a positive integer, got {timeout!r}")

    if payload["role"] == "reviewer":
        stage = payload.get("stage")
        if stage not in VALID_STAGES:
            raise ProtocolError(f"reviewer request.stage must be one of {sorted(VALID_STAGES)}, got {stage!r}")
        if stage != "plan":
            _require_nonempty_str(payload, "candidate_tree")
            _require_nonempty_str(payload, "candidate_readonly_copy")

    if payload["role"] == "builder":
        allowed_paths = payload.get("allowed_paths")
        if allowed_paths is not None and not isinstance(allowed_paths, list):
            raise ProtocolError("request.allowed_paths must be a list when present")

    return payload
