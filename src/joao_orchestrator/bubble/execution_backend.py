"""A0.2 — the single dispatch point every builder/test/reviewer subprocess
must go through (master contract §13, run card §9-10, course-correction D-046).

Before A0.2, `run_sandboxed` (RI-6) was called directly from three different
places (`LocalTestRunner.run`, `GLMBuilder.build`, and — worse —
`CodexCLIReviewer.review_stage`, which bypassed it entirely and called
`subprocess.run` raw). A change to the dispatch/enforcement logic had to be
made correctly in every call site independently, and nothing prevented a new
adapter from adding a fourth, unaudited path. `ExecutionBackend.execute()` is
now the ONLY function in this codebase's `bubble` package allowed to launch an
external subprocess for a builder, test, or reviewer dispatch; a static AST
test (`tests/test_a0_2_corrections.py`) enforces that no adapter calls
`subprocess.run`/`subprocess.Popen`/`run_sandboxed` directly.

`local_untrusted` is the only implemented backend — it delegates to
`sandbox.run_sandboxed` unchanged (same resource limits, timeout-kill sweep,
RSS watchdog, Seatbelt-when-available enforcement; NO claim of holding
against a hostile co-resident program, exactly as `sandbox.py` already
documents). `container` and `vm` are declared stubs: a run that requires one
of them gets `status=BLOCKED, reason_code=PREFLIGHT_UNAVAILABLE` — never a
silent fallback to `local_untrusted`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .sandbox import run_sandboxed

BACKEND_NAMES = ("local_untrusted", "container", "vm")


class ExecutionBackendError(RuntimeError):
    pass


class ExecutionBackend(ABC):
    name = "unknown"
    implemented = False

    @abstractmethod
    def execute(self, argv: list[str], *, cwd: Path, timeout: int,
                network: bool = False,
                environment_allowlist: list[str] | None = None,
                protected: bool = False,
                preserve_host_environment: bool = False,
                extra_read_paths: list[str] | None = None,
                extra_write_paths: list[str] | None = None,
                auth_stage: list[dict[str, str]] | None = None) -> dict[str, Any]: ...


class LocalUntrustedBackend(ExecutionBackend):
    """Missions the operator has already accepted as safe, and JOAO's own
    internal tests. Environment-bounded (Seatbelt when available, fresh temp
    HOME, resource rlimits, RSS watchdog, timeout-kill sweep) but explicitly
    NOT a claim of isolation against a hostile program running with the same
    macOS user's rights — see `sandbox.py` module docstring and the bounded
    A0 claim in the A0.2 audit package."""
    name = "local_untrusted"
    implemented = True

    def execute(self, argv, *, cwd, timeout, network=False, environment_allowlist=None,
                protected=False, preserve_host_environment=False,
                extra_read_paths=None, extra_write_paths=None, auth_stage=None) -> dict[str, Any]:
        result = run_sandboxed(
            argv, cwd=cwd, timeout=timeout, network=network,
            profile_allowlist=environment_allowlist or [], protected=protected,
            preserve_host_environment=preserve_host_environment,
            extra_read_paths=extra_read_paths, extra_write_paths=extra_write_paths,
            auth_stage=auth_stage,
        )
        result.setdefault("backend", self.name)
        return result


class _UnimplementedBackend(ExecutionBackend):
    """container/vm: declared, not implemented in A0.2 (master contract §13,
    run card §9). A protected/critical run that names one of these as its
    `required_backend` fails closed via `preflight_backend`, below — this
    class exists only so `resolve_backend` has a real object to name; its
    `execute()` is never reached by a correctly fail-closed caller, and
    raises loudly if it somehow is."""
    implemented = False

    def execute(self, *args, **kwargs) -> dict[str, Any]:
        raise ExecutionBackendError(
            f"A0.2: backend {self.name!r} is a declared stub, not implemented — "
            "callers must preflight with `preflight_backend()` before ever reaching "
            "execute(); reaching this point is itself a bug, not a runtime condition "
            "a caller should try to handle."
        )


class ContainerBackend(_UnimplementedBackend):
    name = "container"


class VMBackend(_UnimplementedBackend):
    name = "vm"


_BACKENDS: dict[str, ExecutionBackend] = {
    "local_untrusted": LocalUntrustedBackend(),
    "container": ContainerBackend(),
    "vm": VMBackend(),
}


def resolve_backend(name: str) -> ExecutionBackend:
    if name not in _BACKENDS:
        raise ExecutionBackendError(f"unknown ExecutionBackend {name!r} (expected one of {BACKEND_NAMES})")
    return _BACKENDS[name]


def preflight_backend(required_backend: str) -> dict[str, Any]:
    """A0.2 (master contract §13/§18, run card §9): resolve whether
    `required_backend` is actually usable right now. Returns
    `{"ok": True, "backend": <ExecutionBackend>}` when it is, or
    `{"ok": False, "status": "BLOCKED", "reason_code": "PREFLIGHT_UNAVAILABLE",
    "required_backend": ..., "actual_backend": "unavailable"}` when it is
    declared but not implemented — never a silent substitution of
    `local_untrusted` for a run that explicitly asked for stronger isolation."""
    backend = resolve_backend(required_backend)
    if not backend.implemented:
        return {
            "ok": False, "status": "BLOCKED", "reason_code": "PREFLIGHT_UNAVAILABLE",
            "required_backend": required_backend, "actual_backend": "unavailable",
        }
    return {"ok": True, "backend": backend}
