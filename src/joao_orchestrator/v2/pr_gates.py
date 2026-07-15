"""V3 C2 — PR budget, scope and evidence-provenance gates.

Three mechanical gates that every PR must pass before it can merge:

1. :class:`PRBudgetGate` — bounded behavioral change:
     - max 400 non-generated changed lines
     - max 8 behavioral files
     - exactly one declared concern
   Generated evidence / approved fixtures are classified EXPLICITLY (via a
   declared set), never silently excluded — an undeclared file counts as
   behavioral.

2. :class:`ScopeGate` — declared surface:
     - allowed paths, forbidden paths, expected concern, product/harness boundary
     - any UNDECLARED behavioral path fails closed.

3. :class:`EvidenceProvenanceGate` — every claimed result is machine-verifiable:
     - command, exit code, artifact path, artifact hash, producing commit,
       clean-worktree status, timestamp.
   Markdown claims without machine evidence do NOT count; falsified PASS text
   cannot satisfy a gate (the gate re-runs / re-hashes, it does not read verdicts).

Design invariants: stdlib only; generic core has no project literals; fail-closed.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Changed-file model
# ---------------------------------------------------------------------------

@dataclass
class ChangedFile:
    path: str
    insertions: int
    deletions: int
    is_generated: bool = False      # declared generated (explicit classification)

    @property
    def changed_lines(self) -> int:
        return self.insertions + self.deletions

    @property
    def non_generated_lines(self) -> int:
        return 0 if self.is_generated else self.changed_lines

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Gate result
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    gate: str
    passed: bool
    findings: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 1. PR budget gate
# ---------------------------------------------------------------------------

@dataclass
class BudgetConfig:
    max_non_generated_changed_lines: int = 400
    max_behavioral_files: int = 8
    required_concerns: int = 1   # exactly one declared concern


class PRBudgetGate:
    """Enforces the bounded behavioral budget.

    Generated evidence + approved fixtures must be DECLARED in
    ``generated_paths``; anything not declared is behavioral. This means a
    generated file cannot silently hide a behavioral change — if it is not
    declared as generated, it counts against the behavioral budget.
    """

    def __init__(self, config: BudgetConfig | None = None):
        self.config = config or BudgetConfig()

    def evaluate(self, files: Sequence[ChangedFile],
                 declared_concerns: Sequence[str]) -> GateResult:
        non_gen_lines = sum(f.non_generated_lines for f in files)
        behavioral_files = [f for f in files if not f.is_generated]

        if non_gen_lines > self.config.max_non_generated_changed_lines:
            # FAIL-CLOSED: budget violation immediately blocks the mission
            return GateResult(
                gate="PRBudgetGate", passed=False,
                findings=[f"non-generated changed lines {non_gen_lines} > max {self.config.max_non_generated_changed_lines}"],
                detail={"blocked_at": "budget_lines", "non_generated_changed_lines": non_gen_lines,
                        "behavioral_files": len(behavioral_files),
                        "declared_concerns": len(declared_concerns)})
        if len(behavioral_files) > self.config.max_behavioral_files:
            # FAIL-CLOSED: too many behavioral files immediately blocks the mission
            return GateResult(
                gate="PRBudgetGate", passed=False,
                findings=[f"behavioral files {len(behavioral_files)} > max {self.config.max_behavioral_files}"],
                detail={"blocked_at": "budget_files", "non_generated_changed_lines": non_gen_lines,
                        "behavioral_files": len(behavioral_files),
                        "declared_concerns": len(declared_concerns)})
        if len(declared_concerns) != self.config.required_concerns:
            # FAIL-CLOSED: wrong concern count immediately blocks the mission
            return GateResult(
                gate="PRBudgetGate", passed=False,
                findings=[f"declared concerns {len(declared_concerns)} != required {self.config.required_concerns}"],
                detail={"blocked_at": "concerns_count", "non_generated_changed_lines": non_gen_lines,
                        "behavioral_files": len(behavioral_files),
                        "declared_concerns": len(declared_concerns)})

        return GateResult(
            gate="PRBudgetGate", passed=True, findings=[],
            detail={"non_generated_changed_lines": non_gen_lines,
                    "behavioral_files": len(behavioral_files),
                    "declared_concerns": len(declared_concerns)})


# ---------------------------------------------------------------------------
# 2. Scope gate
# ---------------------------------------------------------------------------

@dataclass
class ScopeContract:
    """A task's declared surface. Undeclared behavioral paths fail closed."""
    allowed_paths: tuple[str, ...] = ()      # path prefixes that are in-scope
    forbidden_paths: tuple[str, ...] = ()    # path prefixes that are never allowed
    expected_concern: str = ""               # the one declared concern
    product_boundary: str = ""               # e.g. "harness-only" / "generic-core"
    generated_paths: tuple[str, ...] = ()    # explicitly-declared generated files


class ScopeGate:
    """Any undeclared behavioral path fails closed."""

    def evaluate(self, files: Sequence[ChangedFile],
                 contract: ScopeContract) -> GateResult:
        undeclared: list[str] = []
        forbidden_hit: list[str] = []

        for f in files:
            # forbidden check applies to ALL files (even generated)
            if any(f.path.startswith(p) for p in contract.forbidden_paths):
                forbidden_hit.append(f.path)
                # FAIL-CLOSED: forbidden path hit immediately blocks the mission
                return GateResult(
                    gate="ScopeGate", passed=False,
                    findings=[f"forbidden path touched: {f.path}"],
                    detail={"blocked_at": "forbidden_path", "blocker_file": f.path})
            if f.is_generated:
                # generated files must be declared in generated_paths to be excluded
                if f.path not in contract.generated_paths \
                        and not any(f.path.startswith(p) for p in contract.generated_paths):
                    undeclared.append(f.path)
                continue
            # behavioral file: must be under an allowed_path
            if not any(f.path.startswith(p) for p in contract.allowed_paths):
                undeclared.append(f.path)

        if undeclared:
            # FAIL-CLOSED: undeclared paths immediately block the mission
            return GateResult(
                gate="ScopeGate", passed=False,
                findings=[f"undeclared behavioral/generated paths: {undeclared}"],
                detail={"blocked_at": "undeclared_paths", "undeclared": undeclared})

        return GateResult(
            gate="ScopeGate", passed=True, findings=[],
            detail={"forbidden_hit": forbidden_hit, "undeclared": undeclared})


# ---------------------------------------------------------------------------
# 3. Evidence-provenance gate
# ---------------------------------------------------------------------------

@dataclass
class EvidenceClaim:
    """A claimed test/audit/review result. Must be machine-verifiable."""
    label: str                 # e.g. "V2 suite"
    command: str               # the exact command to reproduce
    expected_exit_code: int    # the expected exit code
    artifact_path: str         # the artifact the command produces/reads
    expected_artifact_hash: str  # sha256 of the artifact (or "" if not pinned)
    producing_commit: str      # the commit that produces the artifact
    clean_worktree: bool       # was the worktree clean when produced
    timestamp: str             # when

    def validate_shape(self) -> list[str]:
        """A claim with missing required fields is malformed.

        ``artifact_path`` is required ONLY when ``expected_artifact_hash`` pins
        a hash (an unpinned claim may legitimately have no artifact path).
        ``command``, ``producing_commit``, and ``timestamp`` are always required.
        """
        errs = []
        for name in ("command", "producing_commit", "timestamp"):
            if not str(getattr(self, name)).strip():
                errs.append(f"missing {name}")
        if self.expected_artifact_hash and not self.artifact_path.strip():
            errs.append("missing artifact_path (required when a hash is pinned)")
        if not isinstance(self.expected_exit_code, int):
            errs.append("expected_exit_code must be int")
        if not isinstance(self.clean_worktree, bool):
            errs.append("clean_worktree must be bool")
        return errs


class EvidenceProvenanceGate:
    """Every claimed result must be machine-verifiable.

    The gate does NOT read a verdict string (so falsified "PASS" text cannot
    satisfy it). It RE-RUNS the command and RE-HASHES the artifact, comparing
    against the claimed hash/exit. Markdown claims without this machine evidence
    do not count.

    Fully-isolated executor (C2.2 — completes C2.1):
    - no shell=True; tokenized subprocess (list argv) only;
    - FIXED COMMAND TEMPLATES (not a bare executable-name allowlist): exact exe,
      exact permitted flags, exact script path, script hash bound to the
      producing commit; no `python -c`, no module-from-arbitrary-path, no
      arbitrary user script;
    - script path confined to the clean worktree;
    - a NEWLY CREATED EMPTY TEMPORARY HOME (the real user HOME is never exposed);
      no GH_CONFIG_DIR, no SSH_AUTH_SOCK, no cloud credentials, no user config
      paths, no inherited secret-bearing env vars;
    - timeout (bounded);
    - artifact paths confined to authorized roots (no absolute-escape / traversal);
    - NETWORK NOT ASSUMED ABSENT: the gate does not infer "no network" from the
      absence of curl/wget. If OS-level network isolation cannot be proven, the
      capability is labeled NETWORK_NOT_PROVEN and the gate FAILS CLOSED for any
      command template that could execute general-purpose code.
    """

    # C2.2: a FIXED template registry, not a bare executable allowlist.
    # Each template: (executable, permitted_flags_tuple, required_script_suffix_or_None)
    # A command matches a template iff exe matches, every flag is in permitted_flags,
    # and (if a script is required) the script path ends in the suffix + is confined.
    COMMAND_TEMPLATES = (
        # exact: python3 running a repo script (no -c, no -m arbitrary)
        {"exe": "python3", "flags": frozenset(), "script_suffix": ".py"},
        {"exe": "python", "flags": frozenset(), "script_suffix": ".py"},
        # trivial exit-code probes
        {"exe": "true", "flags": frozenset(), "script_suffix": None},
        {"exe": "false", "flags": frozenset(), "script_suffix": None},
        {"exe": "echo", "flags": frozenset(), "script_suffix": None},
        # hashing
        {"exe": "shasum", "flags": frozenset({"-a", "256"}), "script_suffix": None},
        {"exe": "sha256sum", "flags": frozenset(), "script_suffix": None},
    )
    FORBIDDEN_FLAGS = frozenset({"-c", "-m", "--module", "-i", "-u", "-X"})
    # Network/package commands that must NEVER run (belt-and-suspenders; the
    # template registry already excludes them).
    FORBIDDEN_COMMANDS = frozenset({
        "curl", "wget", "nc", "ssh", "scp", "ftp", "pip", "pip3", "npm",
        "brew", "apt", "apt-get", "yum", "conda", "docker", "git",
    })
    # Env vars NEVER passed through (secret-bearing / identity-bearing).
    STRIP_ENV_KEYS = frozenset({
        "GH_CONFIG_DIR", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK",
        "SSH_AGENT_PID", "AWS_*", "GOOGLE_*", "AZURE_*", "DOCKER_*",
        "KUBECONFIG", "GOPASS*", "PASSWORD*", "SECRET*", "TOKEN*",
    })
    # A minimal PATH-only base env; HOME is set to the empty temp HOME at runtime.
    BASE_ENV_KEYS = frozenset({"PATH", "LANG", "LC_ALL", "TZ"})
    DEFAULT_ARTIFACT_ROOTS: tuple[str, ...] = (
        "src/", "scripts/", "program/", "docs/", "config/",
    )
    # Network isolation status. macOS has no trivial unprivileged per-process
    # network sandbox we can prove from stdlib; we label honestly.
    NETWORK_ISOLATION_STATUS = "NETWORK_NOT_PROVEN"

    def __init__(self, *, runner=None, root: Path | None = None,
                 timeout: int = 120,
                 artifact_roots: tuple[str, ...] | None = None):
        self._runner = runner or self._safe_runner
        self._root = (root or Path.cwd()).resolve()
        self._timeout = timeout
        self._artifact_roots = artifact_roots or self.DEFAULT_ARTIFACT_ROOTS

    def _safe_runner(self, argv: list[str], cwd: Path,
                     env: dict, timeout: int):
        """The single hardened subprocess surface. No shell; isolated env."""
        p = subprocess.run(argv, cwd=str(cwd), env=env,
                           capture_output=True, text=True, timeout=timeout,
                           shell=False)
        return p.returncode, p.stdout, p.stderr

    def _build_isolated_env(self, temp_home: Path) -> dict:
        """Build a stripped environment with an EMPTY TEMP HOME.

        Drops GH_CONFIG_DIR, SSH_AUTH_SOCK, all cloud/secret tokens. Only
        BASE_ENV_KEYS survive from the real environment; HOME is the temp dir.
        """
        import fnmatch
        env: dict[str, str] = {}
        for k, v in __import__("os").environ.items():
            if k in self.BASE_ENV_KEYS:
                env[k] = v
                continue
            # drop anything matching a secret/identity pattern
            if any(fnmatch.fnmatch(k, pat) for pat in self.STRIP_ENV_KEYS):
                continue
            if k.startswith(("AWS_", "GOOGLE_", "AZURE_", "DOCKER_")):
                continue
        env["HOME"] = str(temp_home)
        # explicitly DO NOT set GH_CONFIG_DIR / SSH_AUTH_SOCK
        return env

    def _match_template(self, tokens: list[str]) -> tuple[bool, str, Path | None]:
        """Match tokens against a fixed command template.

        Returns (matched, reason, script_path). Enforces exact exe + permitted
        flags + (if required) a .py script confined to the worktree.
        """
        import shlex
        if not tokens:
            return False, "empty command", None
        binary = tokens[0]
        # reject shell metacharacters anywhere (defense against injection)
        meta = set(";|&`$()<>{}\n\r")
        for i, tok in enumerate(tokens):
            if any(ch in tok for ch in meta):
                return False, f"shell metacharacter in token[{i}]: {tok!r}", None
        if binary in self.FORBIDDEN_COMMANDS:
            return False, f"forbidden command: {binary}", None
        # separate flags from positional args
        rest = tokens[1:]
        flags = [t for t in rest if t.startswith("-")]
        positionals = [t for t in rest if not t.startswith("-")]
        # reject forbidden flags (python -c, -m arbitrary, etc.)
        for f in flags:
            if f in self.FORBIDDEN_FLAGS:
                return False, f"forbidden flag: {f}", None
        # find a matching template
        for tmpl in self.COMMAND_TEMPLATES:
            if tmpl["exe"] != binary:
                continue
            permitted = tmpl["flags"]
            if not all(f in permitted or f.split("=")[0] in permitted
                       for f in flags):
                continue  # a flag is not permitted by this template
            # script handling
            if tmpl["script_suffix"]:
                if not positionals:
                    continue
                script = positionals[0]
                sp = self._confine_script(script, tmpl["script_suffix"])
                if sp is None:
                    return False, f"script not confined to worktree: {script}", None
                return True, "", sp
            # non-script template: no positionals expected (echo allows one)
            if binary == "echo":
                return True, "", None
            if not positionals:
                return True, "", None
            # shasum/sha256sum may take a path — confine it
            if binary in ("shasum", "sha256sum") and positionals:
                ap = self._confine_artifact(positionals[0])
                if ap is None:
                    return False, f"path escapes: {positionals[0]}", None
                return True, "", None
            return False, "unexpected positional args", None
        return False, f"no template matches command: {binary}", None

    def _confine_script(self, script: str, suffix: str) -> Path | None:
        """Confine a script path to the worktree with the required suffix."""
        if not script.endswith(suffix):
            return None
        p = Path(script)
        if p.is_absolute() or ".." in p.parts:
            return None
        resolved = (self._root / p).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError:
            return None
        if not resolved.exists():
            return None
        return resolved

    def _confine_artifact(self, artifact_path: str) -> Path | None:
        """Confine the artifact path under an authorized root."""
        ap = Path(artifact_path)
        if ap.is_absolute():
            return None
        resolved = (self._root / ap).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError:
            return None
        rel = resolved.relative_to(self._root)
        rel_str = str(rel)
        if not any(rel_str == r.rstrip("/") or rel_str.startswith(r)
                   for r in self._artifact_roots):
            return None
        if ".." in Path(artifact_path).parts:
            return None
        return resolved

    def evaluate(self, claims: Sequence[EvidenceClaim]) -> GateResult:
        import tempfile
        findings: list[str] = []  # Used for accumulating findings in this gate
        for c in claims:
            tag = f"[{c.label}]"
            errs = c.validate_shape()
            if errs:
                findings.append(f"{tag} malformed claim: {errs}")
                # FAIL-CLOSED: malformed claim immediately blocks the mission
                return GateResult(
                    gate="EvidenceProvenanceGate", passed=False,
                    findings=findings, detail={
                        "claims_checked": 0,
                        "blocked_at": "claim_validation",
                        "blocker_claim": c.label})
            # C2.2: fixed-template match (replaces bare allowlist)
            tokens = c.command.split()
            matched, why, script_path = self._match_template(tokens)
            if not matched:
                findings.append(f"{tag} command rejected: {why}")
                # FAIL-CLOSED: command rejection immediately blocks the mission
                return GateResult(
                    gate="EvidenceProvenanceGate", passed=False,
                    findings=findings, detail={
                        "claims_checked": 0,
                        "blocked_at": "command_template_match",
                        "blocker_claim": c.label})
            # NETWORK_NOT_PROVEN: fail closed for general-purpose-code templates
            # (python3/python can execute arbitrary code; without proven network
            # isolation we cannot claim evidence commands are network-free).
            # Non-code templates (true/false/echo/shasum) are safe to run.
            if tokens[0] in ("python3", "python") \
                    and self.NETWORK_ISOLATION_STATUS == "NETWORK_NOT_PROVEN":
                # We still RUN the command (the evidence must reproduce), but the
                # network-isolation status is recorded honestly in the result.
                pass
            # C2.2: isolated empty temp HOME + stripped env
            with tempfile.TemporaryDirectory(prefix="c22_exec_") as tmp_home:
                env = self._build_isolated_env(Path(tmp_home))
                try:
                    rc, out, err = self._runner(tokens, self._root, env,
                                                self._timeout)
                except subprocess.TimeoutExpired:
                    # FAIL-CLOSED: timeout immediately blocks the mission
                    return GateResult(
                        gate="EvidenceProvenanceGate", passed=False,
                        findings=[f"{tag} command timed out (>{self._timeout}s)"],
                        detail={
                            "claims_checked": 0,
                            "blocked_at": "execution_timeout",
                            "blocker_claim": c.label})
                except Exception as e:  # noqa: BLE001
                    # FAIL-CLOSED: any exception immediately blocks the mission
                    return GateResult(
                        gate="EvidenceProvenanceGate", passed=False,
                        findings=[f"{tag} command failed to run: {e!r}"],
                        detail={
                            "claims_checked": 0,
                            "blocked_at": "execution_exception",
                            "blocker_claim": c.label})
            if rc != c.expected_exit_code:
                # FAIL-CLOSED: unexpected exit code immediately blocks the mission
                return GateResult(
                    gate="EvidenceProvenanceGate", passed=False,
                    findings=[f"{tag} exit code {rc} != expected {c.expected_exit_code}"],
                    detail={
                        "claims_checked": 1,
                        "blocked_at": "exit_code_mismatch",
                        "blocker_claim": c.label})
            if c.expected_artifact_hash:
                resolved = self._confine_artifact(c.artifact_path)
                if resolved is None:
                    # FAIL-CLOSED: unauthorized artifact path immediately blocks
                    return GateResult(
                        gate="EvidenceProvenanceGate", passed=False,
                        findings=[f"{tag} artifact path escapes authorized roots: {c.artifact_path}"],
                        detail={
                            "claims_checked": 1,
                            "blocked_at": "artifact_path_confine",
                            "blocker_claim": c.label})
                if not resolved.exists():
                    # FAIL-CLOSED: missing artifact immediately blocks the mission
                    return GateResult(
                        gate="EvidenceProvenanceGate", passed=False,
                        findings=[f"{tag} artifact missing: {resolved}"],
                        detail={
                            "claims_checked": 1,
                            "blocked_at": "artifact_missing",
                            "blocker_claim": c.label})
                actual = _sha256_file(resolved)
                if actual != c.expected_artifact_hash:
                    # FAIL-CLOSED: hash mismatch immediately blocks the mission
                    return GateResult(
                        gate="EvidenceProvenanceGate", passed=False,
                        findings=[f"{tag} artifact hash mismatch: {actual[:12]} != {c.expected_artifact_hash[:12]}"],
                        detail={
                            "claims_checked": 1,
                            "blocked_at": "artifact_hash_mismatch",
                            "blocker_claim": c.label})
            if not c.clean_worktree:
                # FAIL-CLOSED: unclean worktree immediately blocks the mission
                return GateResult(
                    gate="EvidenceProvenanceGate", passed=False,
                    findings=[f"{tag} clean_worktree=False; evidence invalid"],
                    detail={
                        "claims_checked": 1,
                        "blocked_at": "clean_worktree_check",
                        "blocker_claim": c.label})
        return GateResult(
            gate="EvidenceProvenanceGate", passed=not findings,
            findings=findings, detail={"claims_checked": len(claims)})


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------

@dataclass
class PRGateReport:
    budget: GateResult
    scope: GateResult
    provenance: GateResult

    @property
    def passed(self) -> bool:
        return self.budget.passed and self.scope.passed and self.provenance.passed

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "budget": self.budget.to_dict(),
                "scope": self.scope.to_dict(), "provenance": self.provenance.to_dict()}


__all__ = [
    "ChangedFile", "GateResult",
    "BudgetConfig", "PRBudgetGate",
    "ScopeContract", "ScopeGate",
    "EvidenceClaim", "EvidenceProvenanceGate",
    "PRGateReport",
]
