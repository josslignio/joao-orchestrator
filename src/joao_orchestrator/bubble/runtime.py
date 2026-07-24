"""Fail-closed local runtime.  Product data never belongs here."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..domain.models import ProjectProfile
from ..policy.paths import detect_path_violations, detect_sensitive_ignored_files
from ..storage.atomic import FileLock, LockAcquireError, append_line, atomic_write_json, atomic_write_text
from ..integrity.records import (
    CandidateIdentityV2, IntegrityKeyManager, ReviewerRecordV2,
    signed_checkpoint_for_payload,
)
from .sandbox import run_sandboxed
from .execution_backend import ExecutionBackend, LocalUntrustedBackend, preflight_backend
from .candidate import (
    CandidateError, freeze_baseline, freeze_candidate, recompute_candidate_tree,
    release_candidate, verify_candidate_identity,
)
from . import project_registry as project_registry_mod
from .change_capture import EMPTY_DIFF_SHA256, capture_full_diff, ignored_files_inventory
from .reviewer_contract import parse_reviewer_response, validate_reviewer_verdict
from . import promotion as promotion_mod
from . import secure_import as secure_import_mod
from .gates import build_frozen_mission


def _memory_dir() -> Path:
    """Locate the B-28 brain (repo-root `memory/`), honouring an explicit override."""
    override = os.environ.get("JOAO_MEMORY_DIR")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[3] / "memory"


_INJECTOR = None
_INJECTOR_LOADED = False


def _injector():
    """Lazily import the single injection authority (memory/inject.py). None if absent."""
    global _INJECTOR, _INJECTOR_LOADED
    if _INJECTOR_LOADED:
        return _INJECTOR
    _INJECTOR_LOADED = True
    mem = _memory_dir()
    if (mem / "inject.py").exists():
        if str(mem) not in sys.path:
            sys.path.insert(0, str(mem))
        import inject as _inject_mod  # noqa: PLC0415
        _INJECTOR = _inject_mod
    return _INJECTOR


_RETRO = None
_RETRO_LOADED = False


def _retro():
    """Lazily import the Phase-4 retro/loop module (memory/retro.py). None if absent."""
    global _RETRO, _RETRO_LOADED
    if _RETRO_LOADED:
        return _RETRO
    _RETRO_LOADED = True
    mem = _memory_dir()
    if (mem / "retro.py").exists():
        if str(mem) not in sys.path:
            sys.path.insert(0, str(mem))
        import retro as _retro_mod  # noqa: PLC0415
        _RETRO = _retro_mod
    return _RETRO


_LEDGER_SYNC = None
_LEDGER_SYNC_LOADED = False


def _ledger_sync_mod():
    """Lazily import the B-37 ledger→brain sync (memory/ledger_sync.py). None if absent."""
    global _LEDGER_SYNC, _LEDGER_SYNC_LOADED
    if _LEDGER_SYNC_LOADED:
        return _LEDGER_SYNC
    _LEDGER_SYNC_LOADED = True
    mem = _memory_dir()
    if (mem / "ledger_sync.py").exists():
        if str(mem) not in sys.path:
            sys.path.insert(0, str(mem))
        import ledger_sync as _mod  # noqa: PLC0415
        _LEDGER_SYNC = _mod
    return _LEDGER_SYNC


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _git_diff(workspace: Path, ref_a: str, ref_b: str) -> str:
    """A0-3: a plain two-ref diff, used only to render the human-readable
    `baseline-drift.patch` evidence artifact (what was already dirty before
    the run started) — never used as a gate."""
    return subprocess.run(["git", "diff", ref_a, ref_b, "--binary"], cwd=str(workspace),
                          shell=False, capture_output=True, text=True, check=False).stdout


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            value.update(block)
    return value.hexdigest()


def _executable_read_paths(executable: str | Path) -> list[str]:
    """The Seatbelt profile (`sandbox.py::seatbelt_profile`) only grants
    `file-read*` on `cwd`/`home_dir`/`extra_write_paths`/`extra_read_paths`
    plus a fixed set of system directories (`/usr`, `/System`, `/Library`,
    `/opt/homebrew`, `/usr/local`, `/private/var`, ...) — NOT a user's real
    `$HOME` generically. A builder/reviewer CLI installed under
    `~/.local/bin` (verified: this is exactly where the real `joao-glm` and
    `claude` binaries on this host resolve to) is therefore unreadable/
    unexecutable under the bounded sandbox unless its own resolved path is
    explicitly added here — every adapter dispatch below does so, so a real
    dispatch can actually run rather than fail with a sandbox-denied
    "Operation not permitted" on the very first exec.

    Resolves a bare command name (e.g. `"claude"`) via `PATH` the same way
    the sandboxed subprocess itself will; returns `[]` (never a guess) when
    the executable cannot be resolved at all — the dispatch then fails
    exactly as it would have without this helper, never silently different."""
    resolved = shutil.which(str(executable)) or (str(Path(executable).expanduser())
                                                 if Path(executable).expanduser().exists() else None)
    return [resolved] if resolved else []


class RunStatus(str, Enum):
    PENDING = "pending"; PLANNING = "planning"; READY = "ready"
    BUILDING = "building"; TESTING = "testing"; REVIEWING = "reviewing"
    NEEDS_APPROVAL = "needs_approval"; CORRECTING = "correcting"
    PAUSED = "paused"; BLOCKED = "blocked"; FAILED = "failed"
    ACCEPTED = "accepted"; STOPPED = "stopped"


NEXT = {
    # A0.2: PENDING -> BLOCKED is a new edge for a fail-fast preflight refusal
    # (sensitive ignored file already present, or a required ExecutionBackend
    # that is unavailable) — caught before planning, building or reviewing
    # ever start, not merely before promotion.
    RunStatus.PENDING: {RunStatus.PLANNING, RunStatus.STOPPED, RunStatus.BLOCKED},
    RunStatus.PLANNING: {RunStatus.READY, RunStatus.FAILED, RunStatus.STOPPED},
    # C8-B correction: READY -> BLOCKED is a pre-existing edge `run_once()`
    # already tried to take (`"Codex plan review blocked"`) whenever the
    # PRIMARY reviewer implements `review_stage` (e.g. `CodexCLIReviewer`,
    # `GLMReviewer`) and its real "plan" stage verdict is not ok — the
    # default `CodexEvidenceReviewer` has no `review_stage`, so this path
    # was previously unreachable and untested with it. A real reviewer's
    # negative plan verdict is a legitimate, expected outcome, not a
    # programming error; the state machine must be able to record it.
    RunStatus.READY: {RunStatus.BUILDING, RunStatus.PAUSED, RunStatus.STOPPED, RunStatus.BLOCKED},
    RunStatus.BUILDING: {RunStatus.TESTING, RunStatus.BLOCKED, RunStatus.FAILED, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.TESTING: {RunStatus.REVIEWING, RunStatus.FAILED, RunStatus.BLOCKED, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.REVIEWING: {RunStatus.NEEDS_APPROVAL, RunStatus.CORRECTING, RunStatus.BLOCKED, RunStatus.FAILED, RunStatus.PAUSED, RunStatus.STOPPED},
    RunStatus.CORRECTING: {RunStatus.BUILDING, RunStatus.NEEDS_APPROVAL, RunStatus.BLOCKED, RunStatus.FAILED, RunStatus.STOPPED},
    RunStatus.NEEDS_APPROVAL: {RunStatus.ACCEPTED, RunStatus.CORRECTING, RunStatus.STOPPED},
    RunStatus.PAUSED: {RunStatus.READY, RunStatus.BUILDING, RunStatus.TESTING, RunStatus.REVIEWING, RunStatus.STOPPED},
    RunStatus.BLOCKED: {RunStatus.CORRECTING, RunStatus.NEEDS_APPROVAL, RunStatus.STOPPED},
    RunStatus.FAILED: {RunStatus.CORRECTING, RunStatus.STOPPED},
    RunStatus.ACCEPTED: set(), RunStatus.STOPPED: set(),
}


class RuntimeStateError(RuntimeError):
    pass


class BuilderAdapter(ABC):
    # `provider_family` (C8-B, `JOAO_C8_GATE_CONTRACTS.md` G-DBL-AUDIT v4):
    # the coarse identity `bubble.gates.gate_dbl_audit` actually compares —
    # two adapters of the same underlying vendor (e.g. Codex + a GPT-formal
    # import) must never be countable as two independent families. Declared
    # per-class, alongside `provider`/`model`, never caller-settable.
    provider = "unknown"; model = "unknown"; provider_family = "unknown"
    # A0.2 §12.1/§12.2: does this builder's own CLI need to phone its
    # provider (provider_transport_network) independent of the mission's
    # task_network? Declared per-class (not caller-settable) so it cannot be
    # forged by a run argument.
    requires_network_transport = False

    def set_capabilities(self, capabilities: dict[str, Any]) -> None:
        """A0.2 §12.1: the controller hands the FROZEN mission scope to the
        builder fresh before every dispatch (including every correction-loop
        rebuild) — the same scope tests and the reviewer already receive.
        Base implementation just stores it; a builder that needs it (e.g.
        GLMBuilder, to decide its own network permission) reads
        `self._capabilities`. A builder that never calls this (or ignores the
        stored value) simply has no capability-scoped behavior — never a
        silent default grant."""
        self._capabilities = capabilities

    @abstractmethod
    def build(self, mission: str, workspace: Path, run_dir: Path, allowed: list[str], correction: bool) -> dict[str, Any]: ...


class ReviewerAdapter(ABC):
    provider = "unknown"; model = "unknown"; provider_family = "unknown"
    @abstractmethod
    def review(self, run: dict[str, Any], run_dir: Path) -> dict[str, Any]: ...


class TestRunnerAdapter(ABC):
    @abstractmethod
    def run(self, argv: list[str], cwd: Path, timeout: int, *, network: bool = False,
            environment_allowlist: list[str] | None = None, protected: bool = False) -> dict[str, Any]: ...


class MemoryAdapter(ABC):
    @abstractmethod
    def load(self, project_id: str) -> dict[str, Any]: ...


class EventStoreAdapter(ABC):
    @abstractmethod
    def append(self, event: dict[str, Any]) -> None: ...
    @abstractmethod
    def read(self) -> list[dict[str, Any]]: ...


class ProjectProfileAdapter(ABC):
    @abstractmethod
    def load(self, project_id: str, workspace: Path) -> ProjectProfile: ...


class JsonlEvents(EventStoreAdapter):
    def __init__(self, path: Path): self.path = path
    def append(self, event: dict[str, Any]) -> None: append_line(self.path, json.dumps(event, sort_keys=True))
    def read(self) -> list[dict[str, Any]]:
        return [] if not self.path.exists() else [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]


class LocalMemoryAdapter(MemoryAdapter):
    def __init__(self, root: Path): self.root = root
    def load(self, project_id: str) -> dict[str, Any]:
        path = self.root / project_id / "memory.json"
        if not path.exists(): return {"schema_version": 1, "project_id": project_id, "entries": []}
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("schema_version") != 1: raise RuntimeStateError("unsupported memory schema")
        return data


class LocalProfileAdapter(ProjectProfileAdapter):
    def __init__(self, path: Path | None = None): self.path = path
    def load(self, project_id: str, workspace: Path) -> ProjectProfile:
        path = self.path or workspace / ".joao-profile.json"
        if not path.exists():
            return ProjectProfile(project_id=project_id, display_name=project_id, repository_root=str(workspace), allowed_write_paths=[], forbidden_paths=[])
        data = json.loads(path.read_text()); data.setdefault("project_id", project_id); data.setdefault("repository_root", str(workspace))
        allowed = set(ProjectProfile.__dataclass_fields__)
        return ProjectProfile(**{key: value for key, value in data.items() if key in allowed})


class LocalTestRunner(TestRunnerAdapter):
    """RI-6: mission test commands run sandboxed by default (temp HOME, minimal
    env, network denied) — network is allowed only when the run's signed mission
    explicitly declares `network_capability` (RI-6's capability-declaration rule).
    `sandboxed=False` exists only for callers outside a RunRuntime mission that
    need the old passthrough behavior (e.g. ad-hoc scripting).

    A0-5: `protected=True` (a `critical` run) fails closed instead of
    silently degrading to `env-only` enforcement when no real kernel sandbox
    is available — see `sandbox.run_sandboxed`.

    A0.2 (§12.2 single dispatch point): the sandboxed path is routed through
    `ExecutionBackend.execute()`, never `run_sandboxed` directly — the same
    choke point `GLMBuilder` and `CodexCLIReviewer` now use.
    `sandboxed=False` is kept as a literal, un-tracked raw `subprocess.run` —
    it exists ONLY to serve as the "naive/pre-RI-6" baseline inside attack
    tests demonstrating what the sandboxed path prevents (e.g. env leakage, a
    surviving zombie process); `RunRuntime` never constructs a
    `LocalTestRunner` with `sandboxed=False`."""
    def __init__(self, sandboxed: bool = True, backend: ExecutionBackend | None = None):
        self.sandboxed = sandboxed
        self.backend = backend or LocalUntrustedBackend()

    def run(self, argv: list[str], cwd: Path, timeout: int, *, network: bool = False,
            environment_allowlist: list[str] | None = None, protected: bool = False) -> dict[str, Any]:
        if self.sandboxed:
            return self.backend.execute(argv, cwd=cwd, timeout=timeout, network=network,
                                        environment_allowlist=environment_allowlist or [], protected=protected)
        try:
            proc = subprocess.run(argv, cwd=str(cwd), shell=False, capture_output=True, text=True, timeout=timeout)
            return {"argv": argv, "returncode": proc.returncode, "ok": proc.returncode == 0, "stdout": proc.stdout[-16000:], "stderr": proc.stderr[-16000:]}
        except subprocess.TimeoutExpired as exc:
            return {"argv": argv, "returncode": 124, "ok": False, "stdout": (exc.stdout or "")[-16000:], "stderr": (exc.stderr or "")[-16000:], "timed_out": True}


class SandboxBuilder(BuilderAdapter):
    provider = "sandbox"; model = "deterministic-fixture"; provider_family = "sandbox"
    def __init__(self, callback): self.callback = callback
    def build(self, mission, workspace, run_dir, allowed, correction): return self.callback(mission, workspace, correction)


# RI-6: these env var names are explicitly let through the sandbox's
# allowlist for a GLM dispatch (an API key supplied this way, if any).
_GLM_ENV_ALLOWLIST = ["ZAI_API_KEY", "ZHIPU_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY",
                       "JOAO_GLM_MODEL", "JOAO_OPENCODE"]

# Boss directive (2026-07-20, narrow subscription-auth staging): the real
# installed GLM CLI's own capability probe (`scripts/joao_glm_cli.py`
# `AUTH_PATH`) is FILE-resident, not env-var-only as previously documented
# here — verified empirically on this host: `~/.local/share/opencode/
# auth.json` (0600) exists and a fresh-temp-HOME dispatch with no staging
# reports `auth_file_present: False` and fails its own probe. This is the
# ONE file staged into the sandboxed dispatch's temp HOME (see
# `sandbox.run_sandboxed`'s `auth_stage` — source read directly by the
# UNSANDBOXED parent process before the child launches, never inside the
# sandbox profile; the source itself is never modified, only copied) — no
# other opencode config/history/cache is ever staged.
_GLM_AUTH_STAGE = [{"source": str(Path("~/.local/share/opencode/auth.json").expanduser()),
                    "relative_dest": ".local/share/opencode/auth.json"}]

# C8-B temporary topology (Boss decision, 2026-07-20): `ClaudeCodeBuilder` and
# `ClaudeCLIReviewer` are the SAME underlying product (Claude Code CLI) in two
# roles — one shared, controller-owned provider identity, never two
# independently-typed string literals that could drift apart.
CLAUDE_CLI_PROVIDER = "claude-cli"

# The real installed Claude CLI's interactive/subscription session
# authenticates via macOS Keychain + `~/.claude.json` (verified empirically:
# copying `~/.claude.json` alone into a fresh HOME still reports "Not logged
# in" — the actual credential is Keychain-resident, tied to the real user
# session, not a stageable file). Boss directive (2026-07-20, worker-host
# architecture addendum) explicitly forbids BOTH of this CLI's own
# authentication options for a bounded dispatch: `preserve_host_environment`
# (would read the real Keychain session) AND `ANTHROPIC_API_KEY`/`--bare`
# (a paid API account this deployment must never require). No environment
# variable is allowlisted for Claude — the sandboxed dispatch inherits
# nothing beyond `cwd`/`tmp` (a genuinely stripped environment, per that
# directive's authentication section). Net effect, verified rather than
# assumed: under these three constraints simultaneously, a REAL, live,
# network-authenticated Claude Code CLI dispatch is not achievable on a host
# whose only credential is an interactive subscription session — this is a
# structural fact about the installed CLI, not a gap in this adapter's
# implementation, which otherwise dispatches exactly like `GLMBuilder`/
# `GLMReviewer`. `available()` only ever probes for the executable itself
# (matching `GLMReviewer`'s convention) — it does not, and structurally
# cannot, probe whether a live network call would actually authenticate.
_CLAUDE_ENV_ALLOWLIST: list[str] = []

# Staged for completeness/documentation (Boss directive item: "document each
# path" if more than one auth artifact exists) — NOT sufficient on its own:
# `~/.claude.json` holds only non-secret account metadata (`oauthAccount`
# etc.), never the OAuth token itself, which is Keychain-resident (verified
# empirically — see the note above). Staging it changes nothing about the
# "Not logged in" outcome; it is included here only so the ONE real file this
# CLI's config format is known to use is at least handled the same narrow way
# GLM's is, not because it makes live auth work.
_CLAUDE_AUTH_STAGE = [{"source": str(Path("~/.claude.json").expanduser()), "relative_dest": ".claude.json"}]


def _claude_bounded_available(executable: str) -> bool:
    return bool(shutil.which(executable))


# Boss decision (2026-07-20/21): ClaudeCodeBuilder is disabled by STANDING
# PRODUCT POLICY, not merely "the `claude` binary happens to be missing".
# This is the single controller-owned reason string — reused verbatim by
# `ClaudeCodeBuilder.available()`/`unavailable_reason`, `worker_topology.py`,
# `worker_host/server.py`'s dispatch block, and any future UI/CLI surface —
# never a second, independently-worded copy that could drift.
CLAUDE_BUILDER_UNAVAILABLE_REASON = (
    "subscription Keychain authentication cannot be automated safely under current "
    "builder constraints without forbidden builder host-environment passthrough or a "
    "paid API key"
)


class ClaudeCodeBuilder(BuilderAdapter):
    """`BuilderAdapter` for the real Claude Code CLI, dispatched exactly like
    `GLMBuilder` — same `ExecutionBackend`, same fresh-temp-HOME/Seatbelt
    sandbox, same frozen-scope network gating, no second dispatch path, no
    PTY (plain argv-based subprocess, one request/one response).

    DISABLED by standing product policy (Boss decision, 2026-07-20/21) — see
    `CLAUDE_BUILDER_UNAVAILABLE_REASON`. `available()` returns False
    unconditionally: this is a POLICY decision, not merely "the `claude`
    binary happens to be missing on this host" — a future host with the
    binary installed must not silently become selectable again. Reactivation
    requires either a safe official subscription-CLI automation mechanism or
    an explicitly Boss-approved paid API path (`JOAO_C8_GATES_ROADMAP.md`
    "Statut C8-B"), never a host-environment-passthrough workaround.

    Kept fully implemented and tested (not deleted) for that future
    reactivation: dispatch, sandboxing, argv contract and tests all remain
    real and exercised — only `available()` is gated off. Cannot select
    reviewers, cannot write reviewer evidence or a Boss approval record, and
    cannot alter `frozen_mission.json` — it implements exactly the
    `BuilderAdapter.build()` contract `RunRuntime._execute` already enforces
    for every builder (allowed-path violations, one bounded correction, no
    mutation outside `profile.allowed_write_paths`), identically to
    `GLMBuilder`.
    """
    provider = CLAUDE_CLI_PROVIDER
    model = os.environ.get("JOAO_CLAUDE_BUILD_MODEL", "sonnet")
    provider_family = "anthropic"
    requires_network_transport = True
    unavailable_reason = CLAUDE_BUILDER_UNAVAILABLE_REASON

    def __init__(self, executable: str = "claude", backend: ExecutionBackend | None = None, timeout: int = 1200):
        self.executable = executable
        self.timeout = timeout
        self.backend = backend or LocalUntrustedBackend()

    def available(self) -> bool:
        # Disabled by standing policy — never merely "the binary exists".
        # `_claude_bounded_available` is kept as a named, testable probe of
        # the underlying (irrelevant-while-disabled) executable presence,
        # so re-enabling this later is a one-line change with an already
        # correct helper, not a rewrite.
        return False

    def build(self, mission, workspace, run_dir, allowed, correction):
        from .write_tier_policy import assert_write_tier_enabled
        assert_write_tier_enabled("builder.build")
        task = run_dir / ("correction.md" if correction else "builder-task.md")
        atomic_write_text(task, mission)
        output = run_dir / ("claude-correction.jsonl" if correction else "claude-builder.jsonl")
        # `--bare`: Claude's own contract is that this mode NEVER reads
        # Keychain/OAuth, only `ANTHROPIC_API_KEY`/`apiKeyHelper` — chosen
        # deliberately so the auth outcome under this bounded sandbox is
        # deterministic (a clean, honest auth failure absent a key) rather
        # than depending on whatever Seatbelt happens to let a Keychain
        # lookup do. This deployment never sets `ANTHROPIC_API_KEY` (Boss
        # directive) — see the module-level note above `_CLAUDE_ENV_ALLOWLIST`.
        # `--permission-mode acceptEdits`: the builder may apply file edits
        # without an interactive prompt (there is none), the write-capable
        # counterpart to the reviewer's `plan` mode below. `--add-dir
        # workspace` scopes Claude's OWN tool-permission layer to the mission
        # workspace, defense in depth alongside the JOAO sandbox's
        # independent `allowed_write_paths` enforcement.
        argv = [self.executable, "--bare", "-p", mission, "--model", self.model,
                "--permission-mode", "acceptEdits", "--output-format", "json",
                "--add-dir", str(workspace)]
        capabilities = getattr(self, "_capabilities", None) or {}
        # A0.2: the ONLY source of this dispatch's network permission — no
        # adapter-level override, identical discipline to GLMBuilder.
        network = bool(capabilities.get("network_capability", False))
        result = self.backend.execute(argv, cwd=workspace, timeout=self.timeout, network=network,
                                      environment_allowlist=_CLAUDE_ENV_ALLOWLIST,
                                      extra_read_paths=_executable_read_paths(self.executable),
                                      extra_write_paths=[str(run_dir)], auth_stage=_CLAUDE_AUTH_STAGE)
        atomic_write_text(output, result["stdout"])
        # RI-5: never trust a builder-reported hash — the controller
        # (RunRuntime._execute) independently recomputes output_sha256 itself.
        return {"ok": result["ok"], "provider": self.provider, "model": self.model,
                "returncode": result["returncode"], "stdout": result["stdout"][-4000:],
                "stderr": result["stderr"][-4000:], "output": str(output),
                "sandbox_enforcement": result.get("enforcement"),
                "task_network_capability_granted": network,
                "provider_transport_network_declared": self.requires_network_transport}


class GLMBuilder(BuilderAdapter):
    """A0.2 (§12.1/§12.2, run card #2): network permission for this builder's
    own dispatch comes EXCLUSIVELY from the frozen mission scope handed in via
    `set_capabilities()` — never a class-level `network=True` the adapter
    grants itself. GLM's CLI genuinely needs network to reach Z.AI
    (`provider_transport_network`), but on `local_untrusted` there is no
    domain-scoped egress ACL (Seatbelt's `network*` rule is all-or-nothing —
    see `sandbox.py`), so `provider_transport_network` and the mission's own
    `task_network` necessarily collapse onto the SAME enforced toggle here: a
    mission declared `network_capability=False` gets a GLM dispatch with
    network denied too, and — because GLM cannot function without reaching
    Z.AI — that dispatch fails outright rather than silently borrowing
    network access the mission never granted. A real split (GLM allowed to
    reach only Z.AI while the mission's own commands stay network-denied)
    needs domain-scoped egress, which is a `container`/`vm` ExecutionBackend
    property, reported and not implemented in A0.2."""
    provider = "zai-coding-plan"; model = "zai-coding-plan/glm-4.5-air"; provider_family = "zai"
    requires_network_transport = True

    def __init__(self, executable: Path = Path("~/.local/bin/joao-glm").expanduser(), backend: ExecutionBackend | None = None):
        self.executable = executable
        self.backend = backend or LocalUntrustedBackend()

    def build(self, mission, workspace, run_dir, allowed, correction):
        from .write_tier_policy import assert_write_tier_enabled
        assert_write_tier_enabled("builder.build")
        task = run_dir / ("correction.md" if correction else "builder-task.md"); atomic_write_text(task, mission)
        output = run_dir / ("glm-correction.jsonl" if correction else "glm-builder.jsonl")
        argv = [str(self.executable), "--workspace", str(workspace), "--task-file", str(task), "--output", str(output), "--mode", "workspace-write", "--budget", "small"]
        for item in allowed: argv.extend(["--allowed-path", item])
        capabilities = getattr(self, "_capabilities", None) or {}
        # A0.2: the ONLY source of this dispatch's network permission — no
        # adapter-level override. Absent capabilities (set_capabilities()
        # never called) fail closed to no network, never an implicit grant.
        network = bool(capabilities.get("network_capability", False))
        result = self.backend.execute(argv, cwd=workspace, timeout=900, network=network,
                                      environment_allowlist=_GLM_ENV_ALLOWLIST,
                                      extra_read_paths=_executable_read_paths(self.executable),
                                      extra_write_paths=[str(run_dir)], auth_stage=_GLM_AUTH_STAGE)
        # RI-5: never trust a builder-reported hash — the controller
        # (RunRuntime._execute) independently recomputes output_sha256 itself.
        return {"ok": result["ok"], "provider": self.provider, "model": self.model,
                "returncode": result["returncode"], "stdout": result["stdout"][-4000:],
                "stderr": result["stderr"][-4000:], "output": str(output),
                "sandbox_enforcement": result.get("enforcement"),
                "task_network_capability_granted": network,
                "provider_transport_network_declared": self.requires_network_transport}


class CodexEvidenceReviewer(ReviewerAdapter):
    # M0 safe-stop (D-043/C-3): "exact-SHA" is an unqualified claim — the label now states
    # exactly what is proven: a worktree SHA at review time, NOT yet an immutable candidate
    # (that guarantee is RI-3, delivered by A0/M1-A — see SYSTEM_CONSTITUTION_V4.md §4).
    provider = "codex"; model = "worktree-sha-at-review-time"; provider_family = "openai"
    def review(self, run, run_dir):
        proof = run_dir / "review-import.json"
        if not proof.exists():
            # Fail-closed (RI-4/RI-5): an absent proof is not an implicit pass.
            return {"ok": False, "decision": "block", "required": True,
                    "reason": "no review proof imported yet",
                    "expected_candidate_tree": run.get("candidate_tree")}
        return validate_reviewer_verdict(proof.read_text(), expected_candidate_tree=run.get("candidate_tree"),
                                         provider=self.provider, model=self.model)


class GPTFormalEvidenceReviewer(ReviewerAdapter):
    """Secure-import-only reviewer for a formal GPT counter-audit
    (`JOAO_WORKER_INTEGRATION_SPEC.md` §5, pre-C8-B correction #2). Identity
    is fixed by the adapter/controller — NEVER by the imported JSON — exactly
    like `CodexEvidenceReviewer`.

    `provider_family == "openai"`, the SAME family as `CodexCLIReviewer`/
    `CodexEvidenceReviewer`: this reviewer can NEVER satisfy a critical
    tier's second, mutually-distinct family requirement alongside Codex
    (`G_DBL_AUDIT_SAME_FAMILY`) — it is only ever valid as the sole
    `normal`-tier reviewer, or as an additional non-family-critical opinion.

    `inbox_dir` is the controller-owned inbox the orchestrator minted a
    challenge into — never a builder-writable path (see
    `bubble/orchestrator.py` and `bubble/secure_import.py`).
    """
    provider = "openai-gpt"
    model = os.environ.get("JOAO_GPT_MODEL", "gpt-5.6-thinking")
    provider_family = "openai"

    def __init__(self, inbox_dir: Path, import_filename: str = "gpt-review-import.json"):
        self.inbox_dir = Path(inbox_dir)
        self.import_filename = import_filename

    def review_secure(self, *, run_id: str, mission_id: str, candidate_tree: str) -> dict[str, Any]:
        import_path = self.inbox_dir / self.import_filename
        if not import_path.is_file():
            # Fail-closed (RI-4/RI-5): an absent import is not an implicit pass.
            return {"ok": False, "decision": "block", "required": True,
                    "reason": "no GPT-formal review evidence has been imported yet",
                    "expected_candidate_tree": candidate_tree}
        return secure_import_mod.consume_import(
            self.inbox_dir, import_path.read_text(), run_id=run_id, mission_id=mission_id,
            candidate_tree=candidate_tree, expected_reviewer_provider=self.provider,
            expected_model=self.model, now=now())

    def review(self, run, run_dir):
        # mission_id == run_id, ALWAYS (Boss directive, 2026-07-21): the one
        # identifier RunRuntime mints per mission and propagates everywhere —
        # never `project_id` (a prior bug substituted it here, diverging from
        # the builder side, which always used run_id).
        run_id = run.get("run_id", "")
        return self.review_secure(run_id=run_id, mission_id=run_id,
                                  candidate_tree=run.get("candidate_tree", ""))


class ClaudeChatEvidenceReviewer(ReviewerAdapter):
    """Secure-import-only reviewer for a Claude Chat review
    (`JOAO_C8_GATES_SPEC.md` §25.4, WA-03 pulled forward — Boss directive,
    2026-07-20: a live nested `claude` CLI subprocess dispatch from within an
    active Claude Code session is structurally blocked by this harness's own
    safety classifier, independent of any auth/sandbox design; the "chat"
    reviewer round — the Boss manually pastes JOAO's evidence bundle into a
    SEPARATE Claude Chat conversation and the resulting verdict is imported
    back — is therefore Claude's NORMAL/CRITICAL review path today, not a
    fallback). Structurally identical to `GPTFormalEvidenceReviewer`: identity
    is fixed by the adapter/controller — NEVER by the imported JSON.

    `provider_family == "anthropic"`, DISTINCT from `CodexCLIReviewer`/
    `GPTFormalEvidenceReviewer` (`openai`) and from `GLMReviewer`/`GLMBuilder`
    (`zai`) — the genuinely independent second family a critical GLM build
    needs (`JOAO_C8_GATE_CONTRACTS.md` G-DBL-AUDIT, finding GPT v3: Codex+GPT
    are both `openai`, never two distinct families).

    `inbox_dir` is the controller-owned inbox the orchestrator minted a
    challenge into — never a builder-writable path, exactly like
    `GPTFormalEvidenceReviewer`.
    """
    provider = "claude-chat"
    model = os.environ.get("JOAO_CLAUDE_CHAT_MODEL", "claude-chat")
    provider_family = "anthropic"

    def __init__(self, inbox_dir: Path, import_filename: str = "claude-chat-review-import.json"):
        self.inbox_dir = Path(inbox_dir)
        self.import_filename = import_filename

    def review_secure(self, *, run_id: str, mission_id: str, candidate_tree: str) -> dict[str, Any]:
        import_path = self.inbox_dir / self.import_filename
        if not import_path.is_file():
            # Fail-closed (RI-4/RI-5): an absent import is not an implicit pass.
            return {"ok": False, "decision": "block", "required": True,
                    "reason": "no Claude-Chat review evidence has been imported yet",
                    "expected_candidate_tree": candidate_tree}
        return secure_import_mod.consume_import(
            self.inbox_dir, import_path.read_text(), run_id=run_id, mission_id=mission_id,
            candidate_tree=candidate_tree, expected_reviewer_provider=self.provider,
            expected_model=self.model, now=now())

    def review(self, run, run_dir):
        # mission_id == run_id, ALWAYS (Boss directive, 2026-07-21): the one
        # identifier RunRuntime mints per mission and propagates everywhere —
        # never `project_id` (a prior bug substituted it here, diverging from
        # the builder side, which always used run_id).
        run_id = run.get("run_id", "")
        return self.review_secure(run_id=run_id, mission_id=run_id,
                                  candidate_tree=run.get("candidate_tree", ""))


def _extract_codex_final_answer(raw_stdout: str) -> str:
    """C8-B correction (found via the real synthetic NORMAL mission,
    `scripts/c8b_synthetic_normal_mission.py`): `codex exec --json`'s real
    stdout is an NDJSON event stream (`thread.started`/`item.started`/
    `item.completed`/`turn.completed`), not a single bare JSON verdict
    object — the verdict is the LAST `item.completed` event whose
    `item.type == "agent_message"`, in that item's `text` field.

    Tries a direct `json.loads` of the WHOLE string FIRST — every existing
    A0/A0.1/A0.2 test's fake `codex` wrapper prints exactly one bare JSON
    line (`print(json.dumps({...}))`), which already IS a valid single
    verdict object; that exact, already-audited shape must keep working
    unchanged. Only when that direct parse fails does this fall back to
    NDJSON extraction. Returns the original string unchanged if neither
    shape is found — `validate_reviewer_verdict` then reports its own
    unparseable-response BLOCK exactly as it already does today, never a
    fabricated fallback."""
    stripped = raw_stdout.strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        return stripped

    last_text = None
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            last_text = item["text"]
    return last_text if last_text is not None else raw_stdout


class CodexCLIReviewer(ReviewerAdapter):
    """Run a real local Codex review, fail-closed on an ambiguous result.

    A0-1 (correction pass, 2026-07-19): for the "build" and "final" stages —
    the two stages that bind to a frozen candidate — Codex is pointed
    exclusively at `run["candidate"]["readonly_copy"]`, never at
    `run["workspace"]` (which stays live/mutable throughout building,
    correction loops, and human inspection). The candidate's tree hash is
    independently recomputed immediately BEFORE invoking Codex and again
    immediately AFTER it returns; either recompute disagreeing with the
    frozen `candidate_tree` refuses the verdict outright — a tamper either
    just before Codex looked, or while/after it was looking, is caught. Only
    the pre-candidate "plan" stage (which by construction has no candidate
    yet) still reviews `run["workspace"]`.
    """
    provider = "codex-subscription"; model = "local-codex-review"; provider_family = "openai"

    def __init__(self, executable: str = "codex", timeout: int = 900, backend: ExecutionBackend | None = None):
        self.executable = executable
        self.timeout = timeout
        # A0.2 (§12.2 single dispatch point): routed through ExecutionBackend
        # like every other builder/test/reviewer subprocess.
        # `preserve_host_environment=True` below because Codex CLI
        # authenticates via `~/.codex` on the real HOME — a fresh temp HOME
        # (the sandboxed default) would break that; this is a strictly
        # STRONGER guarantee than the pre-A0.2 raw `subprocess.run` call
        # (adds process-group tracking, timeout-kill sweep and OS resource
        # limits — see `sandbox.run_sandboxed`'s `preserve_host_environment`
        # docstring), not a regression.
        self.backend = backend or LocalUntrustedBackend()

    def available(self) -> bool:
        return bool(shutil.which(self.executable) and Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().exists())

    def review_stage(self, run, run_dir, stage: str, active_rules: str = ""):
        # RI-4: the pre-candidate "plan" stage has nothing to bind to yet;
        # "build"/"final" always bind the verdict to the frozen candidate_tree.
        candidate = run.get("candidate") if stage != "plan" else None
        candidate_tree = candidate.get("candidate_tree") if candidate else None

        if stage != "plan":
            # A0-1: no candidate, no review — never silently fall back to the
            # mutable workspace for a stage that is supposed to gate on a
            # frozen candidate.
            if not candidate or not candidate.get("readonly_copy"):
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "A0-1: no frozen candidate readonly_copy available for this review stage"}
            review_root = Path(candidate["readonly_copy"])
            try:
                pre_tree = recompute_candidate_tree(review_root)
            except Exception as exc:  # fail-closed: cannot verify => cannot review
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": f"A0-1: could not verify candidate before review: {type(exc).__name__}: {exc}"}
            if pre_tree != candidate_tree:
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "A0-1: candidate was tampered before the reviewer ever ran",
                        "reviewed_path": str(review_root), "candidate_commit": candidate.get("candidate_commit"),
                        "expected_candidate_tree": candidate_tree, "recomputed_tree_before_review": pre_tree}
        else:
            review_root = Path(run["workspace"])
            pre_tree = None

        output = run_dir / f"codex-{stage}-review.jsonl"
        rules_prefix = (active_rules + "\n\n") if active_rules else ""
        tree_line = f"The candidate under review has candidate_tree = {candidate_tree!r}.\n" if candidate_tree else ""
        contract = (
            "Respond with EXACTLY one JSON object (no markdown fences, no prose before or "
            "after) shaped like: {\"candidate_tree\": \"" + (candidate_tree or "") + "\", "
            "\"verdict\": \"ACCEPT\"|\"P1\"|\"BLOCK\", \"findings\": [\"...\"], "
            "\"reviewer\": {\"provider\": \"codex-subscription\", \"model\": \"<your model>\"}}. "
            "A P1 finding must name the concrete repair."
        )
        prompt = (
            f"{rules_prefix}"
            "You are the independent JOAO reviewer. Work read-only. Review the "
            f"{stage} gate for this bounded mission:\n\n{run['mission']}\n\n"
            f"{tree_line}"
            "Inspect only the current worktree, task evidence and git diff. Do not "
            "edit, commit, push, install packages or call external services. "
            f"{contract}"
        )
        # A0-1: `-C review_root` and `cwd=review_root` are the SAME path Codex
        # is bound to — for build/final that is exclusively the read-only
        # candidate copy, never the mutable workspace.
        argv = [self.executable, "exec", "--json", "--sandbox", "read-only",
                "-C", str(review_root), prompt]
        # A0.2 (§12.2): single dispatch point — `network=True` here is
        # `provider_transport_network` (Codex must reach its own backend to
        # answer at all), never the mission's `task_network`; Codex's own
        # `--sandbox read-only` flag is what actually bounds what this
        # process can touch inside `review_root`.
        dispatch = self.backend.execute(argv, cwd=review_root, timeout=self.timeout,
                                        network=True, preserve_host_environment=True)
        # Independent counter-audit note: a refused dispatch (e.g. the
        # `protected`+`preserve_host_environment` or `protected`+no-Seatbelt
        # guards in `run_sandboxed`) also carries `"pid": None` — check the
        # VALUE, not just key presence, so this stays correct even if a
        # future change ever sets `protected=True` on this call.
        if not dispatch.get("pid"):
            return {"ok": False, "decision": "block", "stage": stage,
                    "reason": f"Codex reviewer unavailable: {dispatch.get('stderr', '')}",
                    "reviewed_path": str(review_root)}

        class _Proc:  # shim so the rest of this method reads exactly as before
            returncode = dispatch.get("returncode", -1)
            stdout = dispatch.get("stdout", "")
            stderr = dispatch.get("stderr", "")
        proc = _Proc()
        atomic_write_text(output, proc.stdout)

        post_tree = None
        if stage != "plan":
            try:
                post_tree = recompute_candidate_tree(review_root)
            except Exception as exc:  # fail-closed: cannot verify => the verdict cannot be trusted
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": f"A0-1: could not verify candidate after review: {type(exc).__name__}: {exc}",
                        "reviewed_path": str(review_root), "candidate_commit": candidate.get("candidate_commit"),
                        "returncode": proc.returncode, "output": str(output), "output_sha256": digest(output)}
            if post_tree != candidate_tree:
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "A0-1: candidate was tampered during or after the review",
                        "reviewed_path": str(review_root), "candidate_commit": candidate.get("candidate_commit"),
                        "expected_candidate_tree": candidate_tree, "recomputed_tree_after_review": post_tree,
                        "returncode": proc.returncode, "output": str(output), "output_sha256": digest(output)}

        answer_text = _extract_codex_final_answer(proc.stdout)
        result = validate_reviewer_verdict(answer_text, expected_candidate_tree=candidate_tree,
                                          provider=self.provider, model=self.model,
                                          returncode=proc.returncode)
        return {**result, "stage": stage, "returncode": proc.returncode, "output": str(output),
                "output_sha256": digest(output), "stderr": proc.stderr[-4000:],
                "reviewed_path": str(review_root),
                "candidate_commit": candidate.get("candidate_commit") if candidate else None,
                "recomputed_tree_before_review": pre_tree, "recomputed_tree_after_review": post_tree}

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


_GLM_REVIEW_CONTRACT = (
    "Respond with EXACTLY one JSON object as your FINAL answer (no markdown fences, "
    "no prose before or after, nothing after it) shaped like: "
    '{"candidate_tree": "<the exact candidate_tree given below>", '
    '"verdict": "ACCEPT"|"P1"|"BLOCK", "findings": ["..."], '
    '"reviewer": {"provider": "zai-coding-plan", "model": "<your model>"}}. '
    "A P1 finding must name the concrete repair."
)


def _extract_glm_final_answer(ndjson_text: str) -> str:
    """`joao-glm`'s `--output` file is OpenCode's raw NDJSON event stream
    (`step_start`/`text`/`tool_use`/`step_finish`, one JSON object per line),
    not a single clean answer. The reviewer's actual verdict is the LAST
    `type == "text"` event's `part.text` — everything else is tool-call
    narration, not the verdict. Returns "" (never a guess) if no text event
    is found; `validate_reviewer_verdict` then correctly rejects it as
    unparseable rather than this function inventing a fallback."""
    last_text = ""
    for line in ndjson_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("type") == "text":
            part = obj.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                last_text = part["text"]
    return last_text


class GLMReviewer(ReviewerAdapter):
    """Mirror of `CodexCLIReviewer`, dispatch target substituted
    (`JOAO_WORKER_INTEGRATION_SPEC.md` §2.1).

    `GLMReviewer.provider == GLMBuilder.provider == "zai-coding-plan"` —
    G-DBL-AUDIT's `G_DBL_AUDIT_BUILDER_SELF_REVIEW` therefore fires whenever
    the orchestrator tries to count this reviewer against a GLM-built
    candidate; it is only ever a valid independent reviewer when the builder
    is NOT GLM.
    """
    provider = "zai-coding-plan"
    model = os.environ.get("JOAO_GLM_MODEL", "zai-coding-plan/glm-4.5-air")
    provider_family = "zai"

    def __init__(self, executable: Path = Path("~/.local/bin/joao-glm").expanduser(),
                 backend: ExecutionBackend | None = None, timeout: int = 900):
        self.executable = executable
        self.timeout = timeout
        # A0.2 (§12.2 single dispatch point): routed through ExecutionBackend
        # like every other builder/test/reviewer subprocess — the same
        # `joao-glm` binary GLMBuilder dispatches, invoked `--mode read-only`.
        self.backend = backend or LocalUntrustedBackend()

    def available(self) -> bool:
        return Path(self.executable).expanduser().is_file()

    def review_stage(self, run, run_dir, stage: str, active_rules: str = ""):
        # Identical candidate-binding discipline to `CodexCLIReviewer`: bind
        # to the frozen read-only candidate copy for build/final, recompute
        # the tree immediately before AND after dispatch, refuse the verdict
        # outright on any mismatch (a tamper just before or during review).
        candidate = run.get("candidate") if stage != "plan" else None
        candidate_tree = candidate.get("candidate_tree") if candidate else None

        if stage != "plan":
            if not candidate or not candidate.get("readonly_copy"):
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "no frozen candidate readonly_copy available for this review stage"}
            review_root = Path(candidate["readonly_copy"])
            try:
                pre_tree = recompute_candidate_tree(review_root)
            except Exception as exc:  # fail-closed: cannot verify => cannot review
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": f"could not verify candidate before review: {type(exc).__name__}: {exc}"}
            if pre_tree != candidate_tree:
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "candidate was tampered before the reviewer ever ran",
                        "reviewed_path": str(review_root), "candidate_commit": candidate.get("candidate_commit"),
                        "expected_candidate_tree": candidate_tree, "recomputed_tree_before_review": pre_tree}
        else:
            review_root = Path(run["workspace"])
            pre_tree = None

        rules_prefix = (active_rules + "\n\n") if active_rules else ""
        tree_line = f"The candidate under review has candidate_tree = {candidate_tree!r}.\n" if candidate_tree else ""
        prompt = (
            f"{rules_prefix}"
            "You are the independent JOAO reviewer. Work read-only. Review the "
            f"{stage} gate for this bounded mission:\n\n{run['mission']}\n\n"
            f"{tree_line}"
            "Inspect only the current worktree, task evidence and git diff. Do not "
            "edit, commit, push, install packages or call external services. "
            f"{_GLM_REVIEW_CONTRACT}"
        )
        task_file = run_dir / f"glm-{stage}-review-task.md"
        atomic_write_text(task_file, prompt)
        output = run_dir / f"glm-{stage}-review.jsonl"

        argv = [str(self.executable), "--workspace", str(review_root), "--task-file", str(task_file),
                "--output", str(output), "--mode", "read-only", "--budget", "small", "--model", self.model]
        # A0.2 (§12.2): `network=True` here is `provider_transport_network`
        # (GLM must reach Z.AI to answer at all), never the mission's own
        # `task_network` — identical framing to `CodexCLIReviewer` above.
        # `joao-glm --mode read-only` additionally denies edit/bash at its
        # own OpenCode permission layer; the JOAO sandbox layer independently
        # never grants write access to `review_root` either (only `run_dir`,
        # for the task/output files themselves) — defense in depth.
        dispatch = self.backend.execute(argv, cwd=review_root, timeout=self.timeout, network=True,
                                        environment_allowlist=_GLM_ENV_ALLOWLIST,
                                        extra_read_paths=_executable_read_paths(self.executable),
                                        extra_write_paths=[str(run_dir)], auth_stage=_GLM_AUTH_STAGE)
        if not dispatch.get("pid"):
            return {"ok": False, "decision": "block", "stage": stage,
                    "reason": f"GLM reviewer unavailable: {dispatch.get('stderr', '')}",
                    "reviewed_path": str(review_root)}

        raw_output = output.read_text() if output.exists() else dispatch.get("stdout", "")
        answer_text = _extract_glm_final_answer(raw_output)
        atomic_write_text(run_dir / f"glm-{stage}-review-answer.txt", answer_text)

        post_tree = None
        if stage != "plan":
            try:
                post_tree = recompute_candidate_tree(review_root)
            except Exception as exc:  # fail-closed: cannot verify => the verdict cannot be trusted
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": f"could not verify candidate after review: {type(exc).__name__}: {exc}",
                        "reviewed_path": str(review_root), "candidate_commit": candidate.get("candidate_commit"),
                        "returncode": dispatch.get("returncode", -1), "output": str(output)}
            if post_tree != candidate_tree:
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "candidate was tampered during or after the review",
                        "reviewed_path": str(review_root), "candidate_commit": candidate.get("candidate_commit"),
                        "expected_candidate_tree": candidate_tree, "recomputed_tree_after_review": post_tree,
                        "returncode": dispatch.get("returncode", -1), "output": str(output)}

        result = validate_reviewer_verdict(answer_text, expected_candidate_tree=candidate_tree,
                                          provider=self.provider, model=self.model,
                                          returncode=dispatch.get("returncode", -1))
        return {**result, "stage": stage, "returncode": dispatch.get("returncode", -1), "output": str(output),
                "stderr": dispatch.get("stderr", "")[-4000:], "reviewed_path": str(review_root),
                "candidate_commit": candidate.get("candidate_commit") if candidate else None,
                "recomputed_tree_before_review": pre_tree, "recomputed_tree_after_review": post_tree}

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


_CLAUDE_REVIEW_CONTRACT = (
    "Your ENTIRE response must be EXACTLY one JSON object and NOTHING else — no "
    "markdown fences, no explanation, no reasoning, no commentary, before or after "
    "it, not even one summary sentence. The first character of your response must "
    "be '{' and the last character must be '}'. Put ALL of your verification "
    "reasoning INSIDE the \"findings\" array as strings (e.g. \"git diff touches "
    "exactly module.py, VALUE 1->2\") — never as a sentence before the JSON. "
    "WRONG (never do this): \"The diff is confirmed as a single-line change... "
    "{\\\"candidate_tree\\\": ...}\" — that leading sentence makes the whole "
    "response unparseable. RIGHT: your response starts immediately with '{' and "
    "that same reasoning goes inside \"findings\" instead. Shaped like: "
    '{"candidate_tree": "<the exact candidate_tree given below, verbatim>", '
    '"verdict": "ACCEPT"|"P1"|"BLOCK", "findings": ["..."], '
    '"reviewer": {"provider": "claude-cli", "model": "<your model>"}}. '
    "A P1 finding must name the concrete repair. Any text outside that single JSON "
    "object makes your entire response unparseable and is treated as a hard "
    "failure, not a partial credit."
)


def _extract_claude_final_answer(raw_stdout: str) -> str:
    """`claude -p --output-format json` wraps the assistant's final message in
    a top-level `{"type": "result", "result": "<text>", ...}` envelope — the
    reviewer's actual verdict is that inner `result` string, not the envelope
    itself. Tries a direct `json.loads` of the whole string first; if that
    parses to a dict with a string `result` field, returns it unwrapped.
    Returns the raw string unchanged (never a fabricated fallback) whenever
    the envelope shape is absent — `validate_reviewer_verdict` then correctly
    rejects it as unparseable rather than this function inventing an answer."""
    stripped = raw_stdout.strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return raw_stdout
    if isinstance(obj, dict) and isinstance(obj.get("result"), str):
        return obj["result"]
    return raw_stdout


class ClaudeCLIReviewer(ReviewerAdapter):
    """Mirror of `CodexCLIReviewer`/`GLMReviewer`, dispatch target substituted
    (`JOAO_WORKER_INTEGRATION_SPEC.md` §2.1, `JOAO_C8_GATES_ROADMAP.md`
    "Cible (débloque le critical GLM)"): `provider_family="anthropic"` is the
    genuinely distinct second family `G-DBL-AUDIT` needs for a critical GLM
    build, since `CodexCLIReviewer`/`GPTFormalEvidenceReviewer` are both
    `openai` (finding GPT v3 — same tool family twice never satisfies
    critical).

    Boss temporary topology (until Codex returns 2026-07-23):
    `ClaudeCLIReviewer` is the FALLBACK-path's PRIMARY-review counterpart and
    the NORMAL-path partner for a GLM-built candidate
    (`bubble/worker_topology.py`). Never a valid reviewer for a Claude-built
    candidate — sharing `provider` (`CLAUDE_CLI_PROVIDER`, the exact controller-
    owned string this class shares with `ClaudeCodeBuilder`, never merely the
    same `provider_family`) fires `G_DBL_AUDIT_BUILDER_SELF_REVIEW`, exactly
    like `GLMReviewer` vs `GLMBuilder` today.

    Boss addendum (2026-07-20, ADD-5): the "no host-environment passthrough"
    rule is scoped to BUILDERS — a builder that can write is exactly where an
    unbounded host-environment dispatch is most dangerous (an escape outside
    `allowed_write_paths` would go completely undetected by Seatbelt, see
    `ClaudeCodeBuilder`'s docstring). A REVIEWER never writes to the
    candidate; its safety instead rests on THREE compensating factors,
    identical to `CodexCLIReviewer`'s already-accepted precedent: (1)
    read-only role — `review_stage` never touches `run["candidate"]`'s files;
    (2) Claude's OWN `--permission-mode plan` is a second, independent
    read-only layer (it never applies an edit/write tool call even if asked);
    (3) the candidate's tree is independently recomputed immediately BEFORE
    and AFTER dispatch — any mutation, from this dispatch or anything else,
    is caught regardless of what OS-level write access the process happened
    to have. `ClaudeCLIReviewer` therefore uses `preserve_host_environment=
    True` (the real Keychain-backed subscription session) — `ClaudeCodeBuilder`
    NEVER does; `tests/test_a0_2_corrections.py` statically asserts this.
    """
    provider = CLAUDE_CLI_PROVIDER
    model = os.environ.get("JOAO_CLAUDE_REVIEW_MODEL", "sonnet")
    provider_family = "anthropic"

    def __init__(self, executable: str = "claude", backend: ExecutionBackend | None = None, timeout: int = 900):
        self.executable = executable
        self.timeout = timeout
        # A0.2 (§12.2 single dispatch point): routed through ExecutionBackend
        # like every other builder/test/reviewer subprocess.
        self.backend = backend or LocalUntrustedBackend()

    def available(self) -> bool:
        # Mirrors CodexCLIReviewer's own convention: executable on PATH AND
        # the real subscription session's config file present — never a
        # fabricated "yes" when neither exists.
        return bool(shutil.which(self.executable)) and Path("~/.claude.json").expanduser().exists()

    def review_stage(self, run, run_dir, stage: str, active_rules: str = ""):
        # Identical candidate-binding discipline to CodexCLIReviewer/GLMReviewer:
        # bind to the frozen read-only candidate copy for build/final,
        # recompute the tree immediately before AND after dispatch, refuse the
        # verdict outright on any mismatch (a tamper just before or during review).
        run_dir = Path(run_dir)  # Ensure run_dir is a Path object
        candidate = run.get("candidate") if stage != "plan" else None
        candidate_tree = candidate.get("candidate_tree") if candidate else None

        if stage != "plan":
            if not candidate or not candidate.get("readonly_copy"):
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "no frozen candidate readonly_copy available for this review stage"}
            review_root = Path(candidate["readonly_copy"])
            try:
                pre_tree = recompute_candidate_tree(review_root)
            except Exception as exc:  # fail-closed: cannot verify => cannot review
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": f"could not verify candidate before review: {type(exc).__name__}: {exc}"}
            if pre_tree != candidate_tree:
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "candidate was tampered before the reviewer ever ran",
                        "reviewed_path": str(review_root), "candidate_commit": candidate.get("candidate_commit"),
                        "expected_candidate_tree": candidate_tree, "recomputed_tree_before_review": pre_tree}
        else:
            review_root = Path(run["workspace"])
            pre_tree = None

        rules_prefix = (active_rules + "\n\n") if active_rules else ""
        tree_line = f"The candidate under review has candidate_tree = {candidate_tree!r}.\n" if candidate_tree else ""
        prompt = (
            f"{rules_prefix}"
            "You are the independent JOAO reviewer. Work read-only. Review the "
            f"{stage} gate for this bounded mission:\n\n{run['mission']}\n\n"
            f"{tree_line}"
            "Inspect only the current worktree, task evidence and git diff. Do not "
            "edit, commit, push, install packages or call external services. "
            f"{_CLAUDE_REVIEW_CONTRACT}"
        )
        # `--bare`: ANTHROPIC_API_KEY-only auth (see class docstring) — no
        # Keychain/OAuth read, so this dispatch can run under a genuinely
        # fresh temp HOME, never `preserve_host_environment`.
        # `--permission-mode plan`: Claude may inspect but never actually
        # applies an edit/write tool call — a read-only review stance enforced
        # at Claude's own tool-permission layer, defense in depth alongside
        # the candidate tree recompute below (never the sole guarantee,
        # exactly like GLMReviewer's `--mode read-only` comment documents for
        # its own dispatch). `--output-format json` yields one parseable
        # envelope instead of interactive/streamed text.
        # NOTE: no `--bare` here (unlike ClaudeCodeBuilder) — `--bare` would
        # explicitly disable the real Keychain/OAuth session this dispatch
        # relies on (see class docstring, ADD-5).
        def _dispatch_one(prompt_text: str, output_path: Path) -> dict[str, Any]:
            argv = [self.executable, "-p", prompt_text, "--model", self.model,
                    "--permission-mode", "plan", "--output-format", "json",
                    "--add-dir", str(review_root)]
            # A0.2 (§12.2): `network=True` here is `provider_transport_network`
            # (Claude must reach its own backend to answer at all), never the
            # mission's own `task_network` — identical framing to
            # `CodexCLIReviewer`/`GLMReviewer` above. `preserve_host_environment=
            # True` (ADD-5, scoped to REVIEWERS only — see class docstring for
            # the three compensating factors): the real Keychain-backed
            # subscription session, never staged/bounded — `ClaudeCodeBuilder`
            # never receives this flag (`tests/test_a0_2_corrections.py` asserts
            # it statically).
            d = self.backend.execute(argv, cwd=review_root, timeout=self.timeout,
                                     network=True, preserve_host_environment=True,
                                     extra_read_paths=_executable_read_paths(self.executable),
                                     extra_write_paths=[str(run_dir)])
            if d.get("pid"):
                atomic_write_text(output_path, d.get("stdout", ""))
            return d

        output = run_dir / f"claude-{stage}-review.jsonl"
        dispatch = _dispatch_one(prompt, output)
        if not dispatch.get("pid"):
            return {"ok": False, "decision": "block", "stage": stage,
                    "reason": f"Claude reviewer unavailable: {dispatch.get('stderr', '')}",
                    "reviewed_path": str(review_root)}

        post_tree = None
        if stage != "plan":
            try:
                post_tree = recompute_candidate_tree(review_root)
            except Exception as exc:  # fail-closed: cannot verify => the verdict cannot be trusted
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": f"could not verify candidate after review: {type(exc).__name__}: {exc}",
                        "reviewed_path": str(review_root), "candidate_commit": candidate.get("candidate_commit"),
                        "returncode": dispatch.get("returncode", -1), "output": str(output)}
            if post_tree != candidate_tree:
                return {"ok": False, "decision": "block", "stage": stage,
                        "reason": "candidate was tampered during or after the review",
                        "reviewed_path": str(review_root), "candidate_commit": candidate.get("candidate_commit"),
                        "expected_candidate_tree": candidate_tree, "recomputed_tree_after_review": post_tree,
                        "returncode": dispatch.get("returncode", -1), "output": str(output)}

        answer_text = _extract_claude_final_answer(dispatch.get("stdout", ""))
        result = validate_reviewer_verdict(answer_text, expected_candidate_tree=candidate_tree,
                                          provider=self.provider, model=self.model,
                                          returncode=dispatch.get("returncode", -1))

        # Bounded format-only retry (Boss directive, 2026-07-21): ONE retry,
        # ONLY when the process exited successfully, the candidate tree is
        # unchanged (already proven above for non-plan stages — this point is
        # unreachable otherwise), AND the response failed SOLELY because the
        # contractual JSON was malformed/prefixed/suffixed with prose
        # (`parse_reviewer_response` returning None — never for a
        # schema-valid-but-wrong-shape response, a P1/BLOCK verdict, a
        # mutation, a timeout, or a non-zero exit). Same candidate
        # commit/tree, same reviewer identity, no builder rerun. Both raw
        # attempts are preserved; the retry count is always visible.
        format_retry_count = 0
        raw_attempts = [dispatch.get("stdout", "")]
        if dispatch.get("returncode", -1) == 0 and parse_reviewer_response(answer_text) is None:
            retry_prompt = (
                f"{prompt}\n\nYour previous response violated the output contract. "
                "Return only the required JSON object. No prose, markdown or code fence."
            )
            retry_output = run_dir / f"claude-{stage}-review-retry.jsonl"
            retry_dispatch = _dispatch_one(retry_prompt, retry_output)
            format_retry_count = 1
            raw_attempts.append(retry_dispatch.get("stdout", ""))
            if retry_dispatch.get("pid"):
                retry_post_tree = post_tree
                retry_tree_ok = True
                retry_mutation_reason = None
                if stage != "plan":
                    try:
                        retry_post_tree = recompute_candidate_tree(review_root)
                    except Exception as exc:  # fail-closed: cannot verify => the retry cannot be trusted
                        retry_tree_ok = False
                        retry_mutation_reason = f"could not verify candidate after format retry: {type(exc).__name__}: {exc}"
                    else:
                        if retry_post_tree != candidate_tree:
                            retry_tree_ok = False
                            retry_mutation_reason = "candidate was tampered during or after the format retry"
                if retry_tree_ok:
                    retry_answer_text = _extract_claude_final_answer(retry_dispatch.get("stdout", ""))
                    result = validate_reviewer_verdict(retry_answer_text, expected_candidate_tree=candidate_tree,
                                                      provider=self.provider, model=self.model,
                                                      returncode=retry_dispatch.get("returncode", -1))
                    post_tree = retry_post_tree
                    dispatch = retry_dispatch
                    output = retry_output
                else:
                    result = {"ok": False, "decision": "block", "schema_valid": False,
                             "reason": retry_mutation_reason, "candidate_commit": candidate.get("candidate_commit"),
                             "expected_candidate_tree": candidate_tree,
                             "recomputed_tree_after_review": retry_post_tree}
            # else: the retry dispatch itself never launched — keep the original
            # malformed-JSON `result` unchanged; never fabricate a pass.

        return {**result, "stage": stage, "returncode": dispatch.get("returncode", -1), "output": str(output),
                "stderr": dispatch.get("stderr", "")[-4000:], "reviewed_path": str(review_root),
                "candidate_commit": candidate.get("candidate_commit") if candidate else None,
                "recomputed_tree_before_review": pre_tree, "recomputed_tree_after_review": post_tree,
                "format_retry_count": format_retry_count, "raw_attempts": raw_attempts}

    def review(self, run, run_dir):
        return self.review_stage(run, run_dir, "final")


def claude_capability() -> dict[str, Any]:
    """Report only a real local Claude CLI; never pretend it is connected."""
    executable = shutil.which("claude")
    return {
        "available": bool(executable),
        "provider": "claude-cli",
        "mode": "optional_secondary_review",
        "reason": "" if executable else "Claude CLI is not installed or not on PATH",
    }


class RunRuntime:
    """Persistent CP2 state machine, evidence writer and bounded repair loop."""
    def __init__(self, state_root: Path, *, builder: BuilderAdapter, reviewer: ReviewerAdapter | None = None, tests: TestRunnerAdapter | None = None, memory: MemoryAdapter | None = None, profiles: ProjectProfileAdapter | None = None, enforce_phase0: bool = False, projects_root: Path | None = None, profiles_root: Path | None = None, ledger_sync: bool = False):
        self.root = Path(state_root).expanduser(); self.builder = builder
        self.reviewer = reviewer or CodexEvidenceReviewer(); self.tests = tests or LocalTestRunner()
        self.memory = memory or LocalMemoryAdapter(self.root / "memory"); self.profiles = profiles or LocalProfileAdapter()
        self.enforce_phase0 = enforce_phase0
        self.ledger_sync = ledger_sync
        self.project_registry = project_registry_mod.ProjectRegistry(
            state_root=self.root, projects_root=projects_root, profiles_root=profiles_root)
        self.projects_root = self.project_registry.resolve_projects_root().path
        self.profiles_root = self.project_registry.resolve_profiles_root().path
        self.integrity_keys = IntegrityKeyManager(self.root / "integrity")
        self.integrity_key = self.integrity_keys.get_key()
    def _dir(self, run_id: str) -> Path: return self.root / "runs" / run_id
    def _inject(self, run: dict[str, Any], role: str, *, files_touched: list[str] | None = None, stage: str | None = None):
        """Compose the role's RÈGLES ACTIVES block, persist it as evidence, log the ids.

        This is the ONE place memory reaches a role prompt — every launch path funnels here.
        If the memory subsystem is genuinely absent the gap is recorded loudly (no silent
        bypass) and an empty block is returned so a mis-installed package still runs.
        """
        folder = self._dir(run["run_id"])
        mod = _injector()
        if mod is None:
            self._event(run, "memory_injection_unavailable", role=role, stage=stage or "")
            from types import SimpleNamespace
            return SimpleNamespace(role=role, ids=[], block="", token_estimate=0)
        result = mod.build_injection(role, project=run.get("project_id", ""),
                                     mission_type=run.get("mission", ""),
                                     files_touched=files_touched)
        name = f"active-rules-{role}" + (f"-{stage}" if stage else "") + ".md"
        atomic_write_text(folder / name, result.block or "(no lessons matched)\n")
        self._event(run, "memory_injected", role=role, stage=stage or "",
                    injected_ids=result.ids, count=len(result.ids),
                    token_estimate=result.token_estimate)
        return result
    def _read(self, run_id: str) -> dict[str, Any]:
        path = self._dir(run_id) / "run.json"
        if not path.exists(): raise RuntimeStateError("unknown run: " + run_id)
        return json.loads(path.read_text())
    def _write(self, run: dict[str, Any]) -> None: atomic_write_json(self._dir(run["run_id"]) / "run.json", run)
    def _events(self, run: dict[str, Any]) -> JsonlEvents: return JsonlEvents(self._dir(run["run_id"]) / "events.jsonl")
    def _event(self, run: dict[str, Any], kind: str, **data) -> None:
        self._events(run).append({"event_id": secrets.token_hex(12), "at": now(), "kind": kind, "run_id": run["run_id"], "status": run["status"], **data})
    def _checkpoint(self, run: dict[str, Any]) -> None:
        folder = self._dir(run["run_id"]) / "checkpoints"
        folder.mkdir(parents=True, exist_ok=True)
        sequence = len([p for p in folder.glob("*.json") if not p.name.endswith(".integrity.json")])
        path = folder / f"{sequence:04d}-{run['status']}.json"
        atomic_write_json(path, run)
        envelope, _ = signed_checkpoint_for_payload(
            run, purpose="runtime-state",
            key=self.integrity_key,
            metadata={"run_id": run["run_id"], "status": run["status"], "sequence": sequence},
        )
        atomic_write_json(path.with_suffix(".integrity.json"), envelope)
        run["last_checkpoint"] = str(path)
        self._write(run)
    def _transition(self, run: dict[str, Any], target: RunStatus, reason: str) -> None:
        old = RunStatus(run["status"])
        if target not in NEXT[old]:
            self._event(run, "invalid_transition", requested=target.value, reason=reason)
            raise RuntimeStateError(f"invalid transition {old.value} -> {target.value}")
        run.update({"status": target.value, "updated_at": now(), "current_step": reason}); self._write(run)
        self._event(run, "state_changed", from_status=old.value, to_status=target.value, reason=reason); self._checkpoint(run)
    def _sync_ledger(self) -> dict | None:
        """B-37: refresh the brain from DEFECTS_LEDGER.md if it changed since the last import."""
        mod = _ledger_sync_mod()
        if mod is None:
            return None
        try:
            return mod.sync_if_stale(lessons=_memory_dir() / "lessons.jsonl",
                                     marker=self.root / "ledger-import-marker.json")
        except Exception as exc:  # a sync failure must never block a mission — inject what we have
            return {"synced": False, "reason": f"{type(exc).__name__}: {exc}"}

    def start(self, *, project_id: str, workspace: Path, mission: str, targeted_tests: list[list[str]], full_tests: list[list[str]], profile: ProjectProfile | None = None, critical: bool = False, recurrence: bool = False, tags: list[str] | None = None, smoke: bool = False, declared_baseline: str | None = None, network_capability: bool = False, read_only: bool = False, required_backend: str = "local_untrusted",
             risk_tier: str | None = None, canary_required: bool = False, spec_sha: str | None = None,
             roadmap_sha: str | None = None, authority_instruction_hash: str | None = None,
             forbidden_paths: list[str] | None = None, criterion_bindings: dict[str, Any] | None = None) -> str:
        ledger_status = self._sync_ledger() if self.ledger_sync else None
        if self.enforce_phase0:
            from .kickoff import spec_is_signed  # noqa: PLC0415
            if not spec_is_signed(self.projects_root, project_id):
                raise RuntimeStateError("Phase 0 non faite — lance le Kickoff")
        workspace = Path(workspace).resolve()
        if not workspace.is_dir() or not (workspace / ".git").exists(): raise RuntimeStateError("workspace must be a local Git worktree")
        if not mission.strip(): raise RuntimeStateError("mission cannot be empty")
        if not full_tests: raise RuntimeStateError("at least one explicit full-test command is required")
        # A0.2 §13 (baseline — one choice, not a caller-selectable OR):
        # critical/protected runs require a genuinely clean workspace;
        # `declared_baseline` (the exceptional dirty-workspace escape hatch)
        # is refused outright for them, never silently honored.
        if critical and declared_baseline:
            raise RuntimeStateError(
                "A0.2: declared_baseline is forbidden for a critical run — critical/protected "
                "missions require a genuinely clean workspace at start(); a declared_baseline "
                "is reserved for non-critical local_untrusted runs and never claims isolation."
            )
        profile = profile or self.profiles.load(project_id, workspace)
        if Path(profile.repository_root).resolve() != workspace: raise RuntimeStateError("profile workspace mismatch")
        baseline_paths = self._paths(workspace)
        # RI-1: refuse to start on a dirty worktree; the only exception is an
        # explicitly declared, frozen temporary baseline (recorded as evidence,
        # never a silent pass-through).
        if baseline_paths and not declared_baseline:
            preview = ", ".join(baseline_paths[:5]) + ("…" if len(baseline_paths) > 5 else "")
            raise RuntimeStateError(
                "RI-1: worktree is not clean (" + preview + ") — start() refuses a dirty "
                "baseline; pass declared_baseline=\"<reason>\" to explicitly freeze and "
                "declare this as an accepted temporary baseline."
            )
        baseline_violations = detect_path_violations(baseline_paths, profile)
        if baseline_violations:
            raise RuntimeStateError("workspace has forbidden or out-of-scope drift: " + "; ".join(baseline_violations))
        run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"; folder = self._dir(run_id); folder.mkdir(parents=True)
        tasks = [{"id": "plan", "status": "pending"}, {"id": "build", "status": "pending", "depends_on": ["plan"]}, {"id": "test", "status": "pending", "depends_on": ["build"]}, {"id": "review", "status": "pending", "depends_on": ["test"]}]
        # A0.2 §12.2/§12.1: `provider_transport_network` is DERIVED from the
        # builder class actually wired in — never a caller argument, so it
        # cannot be forged upward by a run request. `required_backend` and
        # `builder_provider`/`builder_model` are frozen here too, alongside
        # the fields A0-5 already froze (`network_capability`, `read_only`,
        # `profile.allowed_write_paths`) — all cross-checked by
        # `_resolve_mission_scope` before every dispatch.
        run = {"schema_version": 1, "run_id": run_id, "project_id": project_id, "workspace": str(workspace), "mission": mission, "status": "pending", "created_at": now(), "updated_at": now(), "current_step": "created", "profile": profile.to_dict(), "targeted_tests": targeted_tests, "full_tests": full_tests, "corrections_used": 0, "max_corrections": 1, "critical": bool(critical), "recurrence": bool(recurrence), "tags": list(tags or []), "smoke": bool(smoke), "tasks": tasks, "declared_baseline": declared_baseline, "baseline_paths_at_start": baseline_paths, "baseline": None, "network_capability": bool(network_capability), "read_only": bool(read_only), "provider_transport_network": bool(getattr(self.builder, "requires_network_transport", False)), "required_backend": required_backend, "builder_provider": self.builder.provider, "builder_model": self.builder.model}
        atomic_write_text(folder / "mission.md", mission + "\n"); atomic_write_json(folder / "project-profile.json", profile.to_dict())
        atomic_write_json(folder / "task-graph.json", {"tasks": tasks}); atomic_write_json(folder / "plan.json", {"status": "pending", "bounded": True, "max_corrections": 1}); self._write(run)
        # C8-A / G-FROZEN-FINISH-LINE: an immutable frozen_mission.json, written
        # exactly once here (never rewritten by any later call) — the real
        # artefact `bubble/gates.py::gate_frozen_finish_line` compares a run's
        # actual changed paths/corrections against. Written unconditionally
        # (risk_tier defaults to None for callers that don't pass one — the
        # gate itself, not start(), fails closed on a missing risk_tier per D1;
        # start()'s legacy behavior for existing callers is unchanged).
        frozen_mission = build_frozen_mission(
            spec_sha=spec_sha, roadmap_sha=roadmap_sha, authority_instruction_hash=authority_instruction_hash,
            risk_tier=risk_tier, canary_required=canary_required, forbidden_paths=forbidden_paths,
            criterion_bindings=criterion_bindings)
        atomic_write_json(folder / "frozen_mission.json", frozen_mission)
        self._event(run, "run_created", builder_provider=self.builder.provider, builder_model=self.builder.model)

        # A0.2 §13/§18: a run requiring a backend stronger than local_untrusted
        # gets BLOCKED/PREFLIGHT_UNAVAILABLE immediately if that backend is
        # not implemented — never a silent fallback to local_untrusted.
        if required_backend != "local_untrusted":
            backend_check = preflight_backend(required_backend)
            atomic_write_json(folder / "backend-preflight.json", backend_check)
            if not backend_check["ok"]:
                self._event(run, "protected_backend_unavailable", required_backend=required_backend,
                            actual_backend="unavailable", reason_code="PREFLIGHT_UNAVAILABLE")
                self._transition(run, RunStatus.BLOCKED, "A0.2: required ExecutionBackend unavailable (PREFLIGHT_UNAVAILABLE)")
                self._finalize(run)
                return run_id

        # A0.2 §12.4: scan for sensitive gitignored files BEFORE any provider
        # (plan reviewer, builder) is ever invoked — a secret must never be
        # readable by a provider even once.
        if not self._secrets_preflight(run, folder, workspace, "before-plan-review"):
            self._transition(run, RunStatus.BLOCKED, "A0.2: sensitive ignored file present before any provider ran")
            self._finalize(run)
            return run_id

        # A0-3: a declared baseline is genuinely frozen into a real git object
        # BEFORE the builder ever runs — a string label alone proves nothing.
        # `baseline_record` stays None (and no `baseline_frozen` event is ever
        # emitted) unless a resolvable commit object actually exists for it.
        baseline_record: dict[str, Any] | None = None
        if declared_baseline and baseline_paths:
            try:
                baseline_record = freeze_baseline(workspace, run_id, run_dir=folder, hmac_key=self.integrity_key)
            except CandidateError as exc:
                raise RuntimeStateError(f"A0-3: could not freeze declared baseline: {exc}") from exc
            drift_patch = _git_diff(workspace, baseline_record["true_head"], baseline_record["baseline_commit"])
            atomic_write_text(folder / "baseline-drift.patch", drift_patch)
            run["baseline"] = baseline_record
            self._write(run)
        if declared_baseline and baseline_record:
            self._event(run, "baseline_frozen", reason=declared_baseline, paths=baseline_paths,
                        baseline_tree=baseline_record["baseline_tree"], baseline_commit=baseline_record["baseline_commit"])
        elif declared_baseline:
            self._event(run, "baseline_declared_but_nothing_to_freeze", reason=declared_baseline)
        if ledger_status is not None: self._event(run, "ledger_synced", synced=bool(ledger_status.get("synced")), added=ledger_status.get("added", 0), reason=ledger_status.get("reason", ""))
        self._checkpoint(run)
        self._transition(run, RunStatus.PLANNING, "load profile and local memory")
        planner_rules = self._inject(run, "planner")
        atomic_write_json(folder / "memory.json", self.memory.load(project_id))
        atomic_write_json(folder / "plan.json", {"status": "ready", "mission_sha256": hashlib.sha256(mission.encode()).hexdigest(), "bounded": True, "max_corrections": 1, "injected_lesson_ids": planner_rules.ids})
        run["tasks"][0]["status"] = "completed"; self._transition(run, RunStatus.READY, "bounded plan created"); return run_id
    def get(self, run_id: str) -> dict[str, Any]: return self._read(run_id)
    def events(self, run_id: str) -> list[dict[str, Any]]: return self._events(self._read(run_id)).read()
    def pause(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id); self._transition(run, RunStatus.PAUSED, "user pause"); return run
    def resume(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if run["status"] != "paused": raise RuntimeStateError("only paused run can resume")
        prior = [event.get("from_status") for event in self.events(run_id) if event.get("to_status") == "paused"]
        self._transition(run, RunStatus(prior[-1] if prior else "ready"), "user resume"); return run
    def stop(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id); self._transition(run, RunStatus.STOPPED, "user stop"); self._finalize(run); return run
    def approve(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if not run.get("review_verified"):
            self._event(run, "approval_refused", reason="independent review proof is absent or mismatched")
            raise RuntimeStateError("cannot approve without matching independent review proof")
        candidate = run.get("candidate")
        if candidate:
            # RI-3 (attack test 3): re-verify the frozen candidate one last time,
            # immediately before acceptance — approval can happen long after tests
            # and review ran, so this closes the gap a mid-flight tamper check
            # right after testing cannot cover on its own.
            try:
                verify_candidate_identity(candidate, Path(run["workspace"]), self.integrity_key)
                recomputed = recompute_candidate_tree(Path(candidate["readonly_copy"]))
            except Exception as exc:
                self._event(run, "candidate_recompute_failed", stage="approve", error=f"{type(exc).__name__}: {exc}")
                raise RuntimeStateError(f"RI-3: could not re-verify candidate integrity at approval time: {exc}") from exc
            if recomputed != candidate["candidate_tree"]:
                self._event(run, "candidate_integrity_violation", stage="approve",
                           frozen_tree=candidate["candidate_tree"], recomputed_tree=recomputed)
                raise RuntimeStateError("RI-3: candidate was modified after tests/review — evidence invalidated, cannot approve")
        self._transition(run, RunStatus.ACCEPTED, "human approval")
        # A0.2 §15: mint the immutable approval object HERE, at the moment of
        # acceptance — bound to this exact run/candidate/review-evidence
        # triple, written append-only (per-run file + a global ledger),
        # never re-derivable from a later-mutated `run.json` alone.
        if candidate:
            folder = self._dir(run["run_id"])
            review_path = folder / "review-evidence.json"
            review_sha256 = digest(review_path) if review_path.is_file() else ""
            record = promotion_mod.create_approval_record(
                run, candidate, review_proof_sha256=review_sha256,
                approved_by="human", approved_at=now(), hmac_key=self.integrity_key)
            promotion_mod.write_approval_record(folder, self.root / "approvals.jsonl", record)
            self._event(run, "approval_record_written", candidate_tree=candidate["candidate_tree"],
                        review_proof_sha256=review_sha256)
        self._finalize(run); return run
    def reject(self, run_id: str) -> dict[str, Any]: return self.stop(run_id)
    def promote(self, run_id: str, branch: str | None = None) -> dict[str, Any]:
        """A0.2 §15/run card #8: the wired entry point from an accepted run to
        `promotion.promote()` — reads the run's own approval-record.json
        (written by `approve()`) and hands both the run and the record to
        `promote()`, which independently re-verifies every field itself
        before touching git (see `promotion._verify_acceptance_for_promotion`).
        Never bypassable by calling `promotion.promote()` with a bare
        candidate — that call now requires `run`/`approval_record` too."""
        run = self._read(run_id)
        candidate = run.get("candidate")
        if not candidate:
            raise RuntimeStateError("cannot promote a run with no frozen candidate")
        folder = self._dir(run_id)
        approval_path = folder / "approval-record.json"
        if not approval_path.is_file():
            raise RuntimeStateError("cannot promote — no approval-record.json (run was never approve()'d)")
        approval_record = json.loads(approval_path.read_text())
        manifest = promotion_mod.promote(
            Path(run["workspace"]), folder, candidate, run_id,
            branch=branch, run=run, approval_record=approval_record,
            hmac_key=self.integrity_key,
        )
        self._event(run, "promoted", promoted_commit=manifest["promoted_commit"],
                    candidate_tree=manifest["candidate_tree"], promoted_worktree=manifest["promoted_worktree"])
        return manifest
    def _paths(self, workspace: Path) -> list[str]:
        raw = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=str(workspace), shell=False, capture_output=True, text=True, check=True).stdout
        return sorted({item[3:].replace("\\\\", "/") for item in raw.split("\0") if item})
    def _secrets_preflight(self, run: dict[str, Any], folder: Path, workspace: Path, stage: str) -> bool:
        """A0.2 §12.4: scan for sensitive gitignored files at `stage` — called
        before the plan reviewer, before the builder, and (already, via
        A0-4 in `_execute`) after the builder. Writes evidence unconditionally
        (even when clean) so every stage's scan is auditable, not just the
        blocking ones. Returns False (and emits `sensitive_ignored_file_detected`)
        the first time a sensitive-named ignored file is found — the caller
        blocks the run before any provider can read it."""
        ignored = ignored_files_inventory(workspace)
        sensitive = detect_sensitive_ignored_files(ignored)
        atomic_write_json(folder / f"secrets-preflight-{stage}.json",
                          {"stage": stage, "ignored_files": ignored, "sensitive": sensitive})
        if sensitive:
            self._event(run, "sensitive_ignored_file_detected", stage=stage, violations=sensitive)
            return False
        return True
    def _write_retro(self, run: dict[str, Any]) -> None:
        """Phase-4 hook: emit the retro template and record the run metric (state-local).

        Deliberately light — it never fabricates lessons (D-029: candidate ingestion is a
        separate, considered step). Brain runtime-state lives under this runtime's state_root.
        """
        mod = _retro()
        if mod is None:
            return
        # A0.2 §7: redirect the lessons.jsonl WRITE target too, not just the
        # per-run runtime state — resolves to the isolated JOAO_MEMORY_DIR
        # copy under test (see tests/conftest.py), the real committed brain
        # in production (JOAO_MEMORY_DIR unset).
        mod.set_state_dir(self.root / "memory", lessons_path=_memory_dir() / "lessons.jsonl")
        folder = self._dir(run["run_id"]); status = run["status"]; project = run.get("project_id", "")
        template = mod.render_retro_template(project, run["run_id"], (run.get("mission", "")[:80] or "mission"),
                                             spec=run.get("mission", ""), result=f"status={status}")
        atomic_write_text(folder / "retro-template.md", template)
        if status in {"accepted", "stopped", "blocked", "failed"}:
            perfect = status == "accepted" and int(run.get("corrections_used", 0)) == 0 and bool(run.get("review_verified"))
            mod.record_run_metric(project, run["run_id"], perfect=perfect, at=now())
            self._event(run, "retro_recorded", perfect=perfect, runs_until_perfect=mod.runs_until_perfect(project))

    def _finalize(self, run: dict[str, Any]) -> None:
        folder = self._dir(run["run_id"]); atomic_write_json(folder / "final-status.json", {"status": run["status"], "last_checkpoint": run.get("last_checkpoint")})
        self._write_retro(run)
        files = sorted(path for path in folder.rglob("*") if path.is_file() and path.name != "manifest.json")
        atomic_write_json(folder / "manifest.json", {"schema_version": 1, "run_id": run["run_id"], "files": [{"path": str(path.relative_to(folder)), "sha256": digest(path), "bytes": path.stat().st_size} for path in files]})
    def _review_gate(self, run: dict[str, Any], stage: str) -> dict[str, Any]:
        folder = self._dir(run["run_id"])
        changed = []
        changed_path = folder / "changed-paths.json"
        if changed_path.exists():
            changed = json.loads(changed_path.read_text()).get("changed_by_builder", [])
        r_rules = self._inject(run, "reviewer", files_touched=changed, stage=stage)
        if hasattr(self.reviewer, "review_stage"):
            review = self.reviewer.review_stage(run, folder, stage, active_rules=r_rules.block)
        elif stage == "final":
            review = self.reviewer.review(run, folder)
        else:
            review = {"ok": True, "decision": "pass", "stage": stage, "skipped": "legacy reviewer"}
        if stage == "final":
            # RI-4, enforced by the controller regardless of what the reviewer
            # adapter itself claims: a verdict for the wrong (or no) candidate
            # is rejected here even if a buggy/malicious adapter reports ok=True.
            # Scoped to "final" — the stage that actually gates approval; "build"
            # stays an adapter-defined advisory gate (a legacy reviewer with no
            # `review_stage` skips it entirely, as before).
            proof = review.get("proof")
            expected = run.get("candidate_tree")
            if not isinstance(proof, dict) or not expected or proof.get("candidate_tree") != expected:
                review = {
                    "ok": False, "decision": "block", "stage": stage,
                    "reason": "RI-4: reviewer proof missing or bound to a different candidate_tree",
                    "skipped": "RI-4 override",
                    "expected_candidate_tree": expected,
                    "received_candidate_tree": proof.get("candidate_tree") if isinstance(proof, dict) else None,
                    "adapter_claimed_ok": bool(review.get("ok")),
                }
        if stage != "plan" and run.get("candidate") and not review.get("skipped"):
            candidate = run["candidate"]
            try:
                identity = verify_candidate_identity(candidate, Path(run["workspace"]), self.integrity_key)
                proof = review.get("proof")
                if not isinstance(proof, dict):
                    raise RuntimeStateError("review proof is not an object")
                verdict = proof.get("verdict")
                findings = proof.get("findings", [])
                if verdict not in {"ACCEPT", "P1", "BLOCK"} or not isinstance(findings, list):
                    raise RuntimeStateError("review proof is missing a strict verdict/findings")
                returncode = int(review.get("returncode", 0))
                record = ReviewerRecordV2(
                    identity_digest=identity.digest(),
                    identity_signature=candidate["identity_signature"],
                    base_commit=identity.base_commit,
                    parent_commit=identity.parent_commit,
                    candidate_commit=identity.candidate_commit,
                    candidate_tree=identity.candidate_tree,
                    canonical_diff_sha256=identity.canonical_diff_sha256,
                    manifest_sha256=identity.manifest_sha256,
                    reviewer_provider=str(self.reviewer.provider),
                    reviewer_model=str(getattr(self.reviewer, "model", "unknown")),
                    reviewer_return_code=returncode,
                    verdict=str(verdict), findings=list(findings),
                    timestamp=datetime.now(timezone.utc).timestamp(),
                )
                record.validate()
                record.signature = record.compute_signature(self.integrity_key)
                proof.update({
                    "identity_digest": identity.digest(),
                    "identity_signature": candidate["identity_signature"],
                    "reviewer_record_v2": dict(record.__dict__),
                    "review_signature": record.signature,
                })
                review["proof"] = proof
                if returncode != 0:
                    review.update({"ok": False, "decision": "block",
                                   "reason": "reviewer process returned non-zero"})
            except Exception as exc:
                review = {"ok": False, "decision": "block", "stage": stage,
                          "reason": f"M5 reviewer identity binding failed: {type(exc).__name__}: {exc}"}
        atomic_write_json(folder / f"{stage}-review-evidence.json", review)
        self._event(run, "review_completed", stage=stage, provider=self.reviewer.provider,
                    decision=review.get("decision", "block"))
        return review

    def run_once(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if run["status"] == "paused" or run["status"] == "needs_approval": return run
        if run["status"] == "correcting":
            self._transition(run, RunStatus.BUILDING, "correction build")
            return self._execute(run, True, building=True)
        if run["status"] in {"failed", "blocked"}: return self.retry(run_id)
        if run["status"] != "ready": raise RuntimeStateError("run cannot execute from " + run["status"])
        plan_review = self._review_gate(run, "plan")
        if not plan_review.get("ok"):
            self._transition(run, RunStatus.BLOCKED, "Codex plan review blocked"); self._finalize(run); return run
        return self._execute(run, False)
    def retry(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if run["corrections_used"] >= run["max_corrections"]:
            if run["status"] != "needs_approval": self._transition(run, RunStatus.NEEDS_APPROVAL, "correction budget exhausted")
            return run
        self._transition(run, RunStatus.CORRECTING, "bounded repair"); run["corrections_used"] += 1; self._write(run); self._transition(run, RunStatus.BUILDING, "correction build")
        return self._execute(run, True, building=True)
    def _resolve_mission_scope(self, run: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """A0-5: resolve `network_capability`/`read_only`/`allowed_write_paths`
        from the run's OWN frozen `checkpoints/0000-pending.json` — written
        once by `start()`, before any transition, and never rewritten by any
        other code path afterward — rather than from whatever the live `run`
        dict (or a re-read of the on-disk `.joao-profile.json`) currently
        claims. `run.json` is still just a JSON file; an operator or a bug
        with filesystem access could hand-edit its `network_capability` or
        `profile.allowed_write_paths` fields after `start()`. Cross-checking
        every use against the untouched first checkpoint turns a silent,
        undetected scope-widening edit into a fail-closed refusal instead of
        a trusted call argument (RI-7's "never from call args, never from a
        stale static profile" requirement, extended here to also cover a
        tampered live run record — not only a tampered `.joao-profile.json`
        file, which the pre-A0.1 code already handled via the run's frozen
        `profile` snapshot).

        Returns `(scope, mismatches)`: `scope` is None (fail-closed) if the
        checkpoint is missing or disagrees with the live run in any of the
        cross-checked fields; `mismatches` names which fields disagreed.

        A0.2 §12.3 widens the cross-checked field set beyond A0-5's original
        three (`network_capability`, `read_only`, `allowed_write_paths`) to
        the full frozen scope the run card requires: `critical`,
        `environment_allowlist`, `command_timeout_seconds`, `forbidden_paths`,
        `provider_transport_network`, `required_backend`, and builder
        identity (`builder_provider`/`builder_model` — catches a resumed run
        wired to a DIFFERENT builder object than the one `start()` recorded,
        e.g. a process restart with the wrong adapter configured).
        """
        folder = self._dir(run["run_id"])
        frozen_path = folder / "checkpoints" / "0000-pending.json"
        if not frozen_path.exists():
            return None, ["missing checkpoints/0000-pending.json"]
        try:
            frozen = json.loads(frozen_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return None, [f"unreadable checkpoint: {type(exc).__name__}: {exc}"]
        frozen_profile = frozen.get("profile") or {}
        live_profile = run.get("profile") or {}
        mismatches = []
        simple_fields = ("network_capability", "read_only", "critical",
                         "provider_transport_network", "required_backend",
                         "builder_provider", "builder_model")
        for field in simple_fields:
            if frozen.get(field) != run.get(field):
                mismatches.append(field)
        list_fields = ("allowed_write_paths", "environment_allowlist", "forbidden_paths")
        for field in list_fields:
            if list(frozen_profile.get(field) or []) != list(live_profile.get(field) or []):
                mismatches.append(f"profile.{field}")
        if frozen_profile.get("command_timeout_seconds") != live_profile.get("command_timeout_seconds"):
            mismatches.append("profile.command_timeout_seconds")
        # A0.2: the live builder OBJECT this run is actually about to dispatch
        # to must match what was frozen — a resumed process wired to the
        # wrong adapter is a scope violation even if run.json itself was
        # never hand-edited.
        if self.builder.provider != frozen.get("builder_provider") or self.builder.model != frozen.get("builder_model"):
            mismatches.append("live_builder_identity")
        if mismatches:
            return None, mismatches
        return {
            "network_capability": bool(frozen.get("network_capability")),
            "read_only": bool(frozen.get("read_only")),
            "critical": bool(frozen.get("critical")),
            "provider_transport_network": bool(frozen.get("provider_transport_network")),
            "required_backend": frozen.get("required_backend", "local_untrusted"),
            "allowed_write_paths": list(frozen_profile.get("allowed_write_paths") or []),
            "environment_allowlist": list(frozen_profile.get("environment_allowlist") or []),
            "forbidden_paths": list(frozen_profile.get("forbidden_paths") or []),
            "command_timeout_seconds": frozen_profile.get("command_timeout_seconds"),
        }, []

    def _execute(self, run: dict[str, Any], correction: bool, building: bool = False) -> dict[str, Any]:
        workspace, folder = Path(run["workspace"]), self._dir(run["run_id"])
        # `run["status"]` must already be BUILDING before any BLOCKED
        # transition below is legal (BUILDING -> BLOCKED is; READY -> BLOCKED
        # is not) — so the READY -> BUILDING transition always happens first,
        # then the A0-5 scope check, exactly like every other fail-closed
        # check further down in this method.
        if not building: self._transition(run, RunStatus.BUILDING, "builder dispatch")
        scope, scope_mismatches = self._resolve_mission_scope(run)
        if scope is None:
            self._event(run, "mission_scope_tampered_or_unresolvable", mismatches=scope_mismatches)
            self._transition(run, RunStatus.BLOCKED, "A0-5: mission scope diverged from the frozen mission record")
            self._finalize(run); return run
        profile = ProjectProfile(**{key: value for key, value in run["profile"].items() if key in ProjectProfile.__dataclass_fields__})
        profile.allowed_write_paths = scope["allowed_write_paths"]

        # A0.2 §12.4: re-scan for sensitive gitignored files immediately
        # before every builder dispatch (including each correction-loop
        # rebuild) — a mission can be paused/resumed, or a correction loop
        # can span a real time gap, between start()'s preflight scan and this
        # dispatch.
        if not self._secrets_preflight(run, folder, workspace, "before-builder"):
            self._transition(run, RunStatus.BLOCKED, "A0.2: sensitive ignored file present before builder dispatch")
            self._finalize(run); return run

        try:
            with FileLock(folder / "builder", timeout=.01):
                b_rules = self._inject(run, "builder", files_touched=profile.allowed_write_paths)
                mission_for_builder = f"{b_rules.block}\n\n---\n\n{run['mission']}" if b_rules.block else run["mission"]
                # A0.2 §12.1: the frozen scope reaches the builder exactly like
                # it already reaches tests (`network=scope[...]` below) and the
                # reviewer (`active_rules`/candidate binding) — delivered fresh
                # before every dispatch, never assumed from a prior call.
                from .write_tier_policy import assert_write_tier_enabled as _awte
                _awte("RunRuntime._execute")
                if hasattr(self.builder, "set_capabilities"):
                    self.builder.set_capabilities(dict(scope))
                before = self._paths(workspace); builder = self.builder.build(mission_for_builder, workspace, folder, profile.allowed_write_paths, correction)
        except LockAcquireError:
            self._transition(run, RunStatus.BLOCKED, "second builder refused"); self._finalize(run); return run
        except Exception as exc:  # a builder that raises must fail-closed, never strand the run (P1-A)
            self._event(run, "builder_exception", error=f"{type(exc).__name__}: {exc}")
            self._transition(run, RunStatus.BLOCKED, "builder raised; fail-closed"); self._finalize(run); return run
        after = self._paths(workspace); changed = sorted(set(after) - set(before)); violations = detect_path_violations(changed, profile)
        atomic_write_json(folder / "changed-paths.json", {"before": before, "after": after, "changed_by_builder": changed, "violations": violations})
        # RI-5: the controller never trusts a builder-self-reported hash (e.g. a
        # builder-authored SHA256SUMS-style claim) — any such field is dropped
        # here and, when the builder names an output file, independently re-hashed.
        builder_evidence = {key: value for key, value in builder.items()
                            if key not in {"output_sha256", "sha256", "candidate_tree"}}
        output_path = builder.get("output")
        if output_path and Path(output_path).exists():
            builder_evidence["controller_verified_output_sha256"] = digest(Path(output_path))
        atomic_write_json(folder / "builder-evidence.json", builder_evidence)
        if not builder.get("ok") or violations:
            self._transition(run, RunStatus.BLOCKED, "builder failed or outside scope"); self._finalize(run); return run

        # RI-2: complete change capture — tracked+staged+untracked+deleted+
        # renamed+permissions+symlinks, cross-validated against an
        # independently captured status list. `git diff` alone is never proof.
        capture = capture_full_diff(workspace)
        atomic_write_json(folder / "change-capture-evidence.json", {
            "changed_paths": capture["changed_paths"], "completeness_ok": capture["completeness_ok"],
            "missing_from_diff": capture["missing_from_diff"], "extra_in_diff": capture["extra_in_diff"],
        })
        if not capture["completeness_ok"]:
            self._event(run, "change_capture_incomplete", missing=capture["missing_from_diff"])
            self._transition(run, RunStatus.BLOCKED, "RI-2: diff completeness check failed"); self._finalize(run); return run
        atomic_write_text(folder / "final-diff.patch", capture["patch"].decode(errors="replace"))
        run["final_diff_sha256"] = digest(folder / "final-diff.patch")

        # A0-3: when this run started from a genuinely frozen exceptional
        # baseline, isolate exactly what the builder itself changed (diffed
        # against the frozen baseline_commit, not the live/movable branch
        # HEAD) — this is what makes a pre-existing modification
        # distinguishable from the builder's own work, instead of both being
        # silently merged into a single "the builder did this" patch above.
        baseline = run.get("baseline")
        if baseline:
            builder_capture = capture_full_diff(workspace, base_ref=baseline["baseline_commit"])
            atomic_write_text(folder / "builder-only-diff.patch", builder_capture["patch"].decode(errors="replace"))
            run["builder_only_diff_sha256"] = digest(folder / "builder-only-diff.patch")
            self._event(run, "builder_diff_isolated_from_baseline", baseline_commit=baseline["baseline_commit"],
                        builder_only_diff_sha256=run["builder_only_diff_sha256"])

        # A0-4: inventory every gitignored file actually present on disk —
        # `git add -A`/`git status`/`git diff` never see these paths at all,
        # so without this dedicated check a sensitive-looking ignored file
        # (e.g. `payload.secret` next to a `*.secret` .gitignore rule) would
        # silently ride along through build, review, and promotion.
        ignored = ignored_files_inventory(workspace)
        atomic_write_json(folder / "ignored-files-evidence.json", {"ignored_files": ignored})
        sensitive_ignored = detect_sensitive_ignored_files(ignored)
        if sensitive_ignored:
            self._event(run, "sensitive_ignored_file_detected", violations=sensitive_ignored)
            self._transition(run, RunStatus.BLOCKED, "A0-4: gitignored file(s) in a sensitive runtime path"); self._finalize(run); return run

        # RI-8: an empty diff is FAILED for a change mission — acceptable only
        # when the mission explicitly declared itself read_only. A0-5: taken
        # from the resolved (frozen-checkpoint-verified) scope, not the live
        # run dict directly.
        if run["final_diff_sha256"] == EMPTY_DIFF_SHA256 and not scope["read_only"]:
            run["tasks"][1]["status"] = "completed"; self._write(run)
            self._event(run, "nothing_produced", final_diff_sha256=run["final_diff_sha256"])
            self._transition(run, RunStatus.FAILED, "RI-8: empty diff for a non-read_only mission"); self._finalize(run); return run
        run["tasks"][1]["status"] = "completed"; self._write(run)

        # RI-3: freeze the immutable candidate before anything reviews or tests
        # it. A prior candidate from an earlier correction attempt (if any) is
        # explicitly invalidated — it is never presented as still valid.
        attempt = run["corrections_used"] + 1
        prior_candidate = run.get("candidate")
        if prior_candidate:
            self._event(run, "candidate_invalidated", prior_candidate_tree=prior_candidate.get("candidate_tree"),
                        reason="a new build attempt supersedes it")
            try:
                release_candidate(prior_candidate, workspace)
            except Exception:
                pass
        try:
            candidate = freeze_candidate(
                workspace, folder, run["run_id"], attempt,
                base_commit=(run.get("baseline") or {}).get("baseline_commit"),
                hmac_key=self.integrity_key,
            )
        except Exception as exc:  # a freeze failure (incl. a raw git CalledProcessError) must
            # fail-closed, never strand the run in BUILDING (same principle as P1-A above).
            self._event(run, "candidate_freeze_failed", error=f"{type(exc).__name__}: {exc}")
            self._transition(run, RunStatus.BLOCKED, "RI-3: could not freeze immutable candidate"); self._finalize(run); return run
        run["candidate"] = candidate; run["candidate_tree"] = candidate["candidate_tree"]; self._write(run)
        atomic_write_json(folder / "candidate-evidence.json", candidate)
        self._event(run, "candidate_frozen", candidate_tree=candidate["candidate_tree"], attempt=attempt)

        build_review = self._review_gate(run, "build")
        if build_review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]:
            # Bounded repair (Boss negative-matrix: "more than one correction"
            # must be refused): the budget is consumed HERE, at the moment a
            # P1 verdict is actually granted a rebuild — previously this
            # branch transitioned to CORRECTING without ever incrementing
            # corrections_used, so a reviewer returning P1 forever could
            # rebuild indefinitely (the budget check `0 < 1` never became
            # false). Mirrors `retry()`'s own accounting exactly.
            run["corrections_used"] += 1
            self._transition(run, RunStatus.CORRECTING, "Codex build review P1; one repair permitted"); return run
        if not build_review.get("ok"):
            self._transition(run, RunStatus.BLOCKED, "Codex build review blocked"); self._finalize(run); return run

        candidate_copy = Path(candidate["readonly_copy"])
        self._transition(run, RunStatus.TESTING, "targeted and full tests")
        # A0-5: network capability taken from the resolved scope (frozen
        # checkpoint), never straight from the live run dict.
        results = [self.tests.run(argv, candidate_copy, profile.command_timeout_seconds,
                                  network=scope["network_capability"],
                                  environment_allowlist=profile.environment_allowlist,
                                  protected=bool(run.get("critical")))
                  for argv in run["targeted_tests"] + run["full_tests"]]
        atomic_write_json(folder / "test-results.json", {"results": results, "all_passed": all(item["ok"] for item in results)})
        if not all(item["ok"] for item in results): self._transition(run, RunStatus.FAILED, "tests failed"); self._finalize(run); return run

        # RI-3 (attack test 3): re-verify the frozen candidate was not modified
        # while tests were running, before trusting the test results at all.
        try:
            recomputed_tree = recompute_candidate_tree(candidate_copy)
        except Exception as exc:  # can't verify integrity => fail-closed, never strand the run
            self._event(run, "candidate_recompute_failed", stage="post-test", error=f"{type(exc).__name__}: {exc}")
            self._transition(run, RunStatus.BLOCKED, "RI-3: could not re-verify candidate integrity"); self._finalize(run); return run
        if recomputed_tree != candidate["candidate_tree"]:
            self._event(run, "candidate_integrity_violation", stage="post-test",
                        frozen_tree=candidate["candidate_tree"], recomputed_tree=recomputed_tree)
            self._transition(run, RunStatus.BLOCKED, "RI-3: candidate tampered after tests — evidence invalidated"); self._finalize(run); return run

        run["tasks"][2]["status"] = "completed"; self._transition(run, RunStatus.REVIEWING, "independent review")
        review = self._review_gate(run, "final"); atomic_write_json(folder / "review-evidence.json", review)
        proof = review.get("proof", {})
        run["final_review_proof"] = proof if isinstance(proof, dict) else {}
        run["review_verified"] = bool(
            review.get("ok") and proof.get("verdict") == "ACCEPT"
            and proof.get("candidate_tree") == candidate["candidate_tree"]
            and proof.get("identity_digest") == candidate.get("identity_digest")
            and proof.get("review_signature")
        )
        self._write(run)
        if review.get("decision") == "p1" and run["corrections_used"] < run["max_corrections"]:
            # Same bounded-repair accounting fix as the build-review P1
            # branch above — consume the budget at grant time.
            run["corrections_used"] += 1
            self._transition(run, RunStatus.CORRECTING, "P1; one repair permitted"); return run
        if not review.get("ok") or review.get("decision") == "block": self._transition(run, RunStatus.BLOCKED, "review blocked"); self._finalize(run); return run
        run["tasks"][3]["status"] = "completed"; self._transition(run, RunStatus.NEEDS_APPROVAL, "review completed; human decision"); self._finalize(run); return run
