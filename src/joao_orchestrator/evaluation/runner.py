"""Deterministic evaluation harness — bounded command runner (C1).

Runs local deterministic commands for one evaluation subject and produces an
integrity-signed :class:`EvaluationReport`. The runner is the only component
that executes subprocesses in the evaluation flow.

Design invariants (see repository AGENTS/invariants):

* argv arrays only, ``shell=False`` always, explicit cwd/env/timeout.
* cwd is confined to the subject root; symlink and traversal escapes fail
  closed.
* The environment is sanitized: provider/token variables are stripped
  (``sanitize_environment``), and secret-like ``extra_env`` keys are rejected
  before any subprocess or persistence.
* Token-like values leaked into captured stdout/stderr are redacted.
* Forbidden executables (shells, package managers, network tools) and
  destructive Git subcommands are rejected at argv-validation time.
* Runtime state lives outside managed repositories. The runtime root is
  validated against an explicit tuple of managed repository roots.
* Artifacts are written atomically (write-tmp + fsync + os.replace) by
  reusing :mod:`joao_orchestrator.storage.atomic`; canonical (sorted, compact,
  ``allow_nan=False``) JSON is used so integrity hashes are reproducible.
* Timeout, non-zero exit, missing artifacts and missing/unknown/non-finite
  metrics all fail closed into the report (``passed=False``).

Standard library only. No provider, no network.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Mapping

from ..storage.atomic import atomic_write_bytes
from .models import (
    CommandResult,
    EvaluationReport,
    EvaluationSpec,
    MetricResult,
    canonical_json_bytes,
    is_secret_env_name,
    validate_safe_id,
)

FORBIDDEN_EXECUTABLES = {
    "sh",
    "bash",
    "zsh",
    "dash",
    "fish",
    "cmd",
    "cmd.exe",
    "powershell",
    "pwsh",
    "curl",
    "wget",
    "pip",
    "pip3",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "brew",
    "apt",
    "apt-get",
}
FORBIDDEN_GIT_SUBCOMMANDS = {
    "clean",
    "reset",
    "rebase",
    "merge",
    "push",
    "checkout",
    "switch",
    "branch",
}
# Forbidden module names reachable via interpreters with "-m <name>".
# Closes the `python -m pip install ...` bypass (Codex defect 1).
FORBIDDEN_MODULES = {
    "pip",
    "pip3",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "ensurepip",
    "venv",
    "http.client",
    "urllib.request",
    "urllib",
    "requests",
    "httpx",
    "wget",
    "curl",
    "socketserver",
    "webbrowser",
}
# Interpreter base names that may launch forbidden modules via "-m".
_INTERPRETER_BASES = {
    "python", "python2", "python3", "python3.8", "python3.9", "python3.10",
    "python3.11", "python3.12", "python3.13", "python3.14", "py",
    "node", "nodejs", "ruby", "php",
}
# Global git options that may precede the subcommand (Codex defect 2).
# Conservative: if we cannot positively identify a safe subcommand, we reject.
_GIT_GLOBAL_OPTION_PREFIXES = ("-",)


class EvaluationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_environment(
    base: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = dict(os.environ if base is None else base)
    for key in list(source):
        if is_secret_env_name(key):
            source.pop(key, None)
    if extra:
        for key, value in extra.items():
            if is_secret_env_name(key):
                raise EvaluationError(f"refusing secret-like environment key: {key}")
            source[key] = value
    source["PYTHONHASHSEED"] = "0"
    source["LC_ALL"] = "C"
    source["LANG"] = "C"
    return source


def _validate_argv(argv: tuple[str, ...]) -> None:
    if any(part in {"--force", "--force-with-lease", "--no-force"} for part in argv):
        raise EvaluationError("force-related flag is forbidden")

    executable = Path(argv[0]).name.lower()
    if executable in FORBIDDEN_EXECUTABLES:
        raise EvaluationError(f"forbidden executable: {executable}")

    # Close the `python -m pip install ...` bypass (Codex defect 1, hardened in
    # final review against bundled short flags like `-Im`). When an interpreter
    # is the entry point, we look for a module flag (-m / --module, possibly
    # bundled in a short-flag group such as -Im, or in --module=NAME form) and
    # reject forbidden package-manager / network modules.
    if executable in _INTERPRETER_BASES:
        module_name: str | None = None
        tokens = list(argv[1:])
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--":
                break  # end of options; remaining tokens are operands
            if tok in {"-m", "--module"}:
                if i + 1 < len(tokens):
                    module_name = tokens[i + 1]
                break
            if tok.startswith("--module="):
                module_name = tok[len("--module="):]
                break
            if tok.startswith("-") and not tok.startswith("--") and len(tok) >= 2:
                # Bundled short flags. If 'm' appears anywhere in the group,
                # Python treats the following argument as the module name.
                if "m" in tok[1:]:
                    if i + 1 < len(tokens):
                        module_name = tokens[i + 1]
                    break
            i += 1
        if module_name is not None:
            root_mod = module_name.lower().split(".")[0]
            if root_mod in FORBIDDEN_MODULES:
                raise EvaluationError(
                    f"forbidden module via interpreter: {module_name}"
                )

    # Git global options (e.g. `git -C repo reset --hard`) may precede the
    # subcommand. To avoid misreading an option's value as the subcommand, we
    # use a known allowlist of git global options (with their value-taking
    # behavior) and FAIL CLOSED on any unknown leading global option (Codex
    # defect 2, hardened in final review).
    _GIT_GLOBAL_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                              "--exec-path", "--super-prefix", "--config-env", "--output"}
    _GIT_GLOBAL_FLAG_OPTS = {"--bare", "--no-replace-objects", "--literal-pathspecs",
                             "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
                             "--no-optional-locks", "--paginate", "--no-pager",
                             "-p", "-P", "--help", "-l"}
    if executable == "git":
        subcommand: str | None = None
        tokens = list(argv[1:])
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "--":
                i += 1
                if i < len(tokens):
                    subcommand = tokens[i].lower()
                break
            if token.startswith("-"):
                # Self-contained --opt=value form: consumes only itself.
                if "=" in token:
                    i += 1
                    continue
                if token in _GIT_GLOBAL_VALUE_OPTS:
                    if i + 1 < len(tokens):
                        i += 2
                    else:
                        i += 1
                    continue
                if token in _GIT_GLOBAL_FLAG_OPTS:
                    i += 1
                    continue
                # Unknown leading global option -> fail closed.
                raise EvaluationError(
                    f"unknown git global option is forbidden: {token}"
                )
            # First non-option token is the subcommand.
            subcommand = token.lower()
            break
        if subcommand is None:
            raise EvaluationError("git invocation without a subcommand is forbidden")
        if subcommand in FORBIDDEN_GIT_SUBCOMMANDS:
            raise EvaluationError(f"forbidden git subcommand: {subcommand}")


def _redact_environment_secrets(value: str, source_env: Mapping[str, str]) -> str:
    redacted = value
    secret_values = {
        item for key, item in source_env.items()
        if is_secret_env_name(key) and isinstance(item, str) and len(item) >= 6
    }
    for secret in sorted(secret_values, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _tail(value: str, max_chars: int, source_env: Mapping[str, str]) -> str:
    value = _redact_environment_secrets(value, source_env)
    if len(value) <= max_chars:
        return value
    return value[-max_chars:]


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


class EvaluationRunner:
    def __init__(
        self,
        runtime_root: Path,
        managed_repositories: tuple[Path, ...] = (),
        *,
        clock: Callable[[], str] = utc_now,
        tail_chars: int = 8_000,
    ) -> None:
        self.runtime_root = runtime_root.expanduser().resolve()
        self.managed_repositories = tuple(path.expanduser().resolve() for path in managed_repositories)
        self.clock = clock
        self.tail_chars = tail_chars
        self._validate_runtime_root()

    def _validate_runtime_root(self) -> None:
        for repository in self.managed_repositories:
            if _is_relative_to(self.runtime_root, repository):
                raise EvaluationError(
                    f"runtime_root must be outside managed repository: {repository}"
                )

    def _atomic_write_json(self, path: Path, payload: object) -> None:
        # Reuse the canonical atomic-write primitive (write-tmp + fsync +
        # os.replace) so we never duplicate the durability contract. We supply
        # canonical (sorted, compact, allow_nan=False) bytes ourselves because
        # integrity hashes demand reproducible serialization.
        atomic_write_bytes(path, canonical_json_bytes(payload) + b"\n")

    def _append_event(self, path: Path, event: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(canonical_json_bytes(dict(event)))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _load_metrics(self, subject_root: Path, spec: EvaluationSpec) -> tuple[MetricResult, ...]:
        lexical_metrics_path = subject_root / spec.metrics_file
        if lexical_metrics_path.is_symlink():
            raise EvaluationError("metrics_file must not be a symlink")
        metrics_path = lexical_metrics_path.resolve()
        if not _is_relative_to(metrics_path, subject_root):
            raise EvaluationError("metrics_file escapes subject_root")
        try:
            raw = json.loads(metrics_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EvaluationError(f"missing metrics file: {spec.metrics_file}") from exc
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"invalid metrics JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise EvaluationError("metrics file must contain a JSON object")

        expected = {rule.name for rule in spec.metrics}
        actual = set(raw)
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        if missing:
            raise EvaluationError(f"missing metrics: {', '.join(missing)}")
        if unknown:
            raise EvaluationError(f"unknown metrics: {', '.join(unknown)}")

        results: list[MetricResult] = []
        for rule in spec.metrics:
            value = raw[rule.name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EvaluationError(f"metric {rule.name} must be numeric")
            value = float(value)
            if not math.isfinite(value):
                raise EvaluationError(f"metric {rule.name} must be finite")
            results.append(
                MetricResult(name=rule.name, value=float(value), source=spec.metrics_file)
            )
        return tuple(results)

    def run(
        self,
        spec: EvaluationSpec,
        subject_root: Path,
        *,
        subject_label: str,
        revision: str,
    ) -> EvaluationReport:
        spec.validate()
        validate_safe_id(subject_label, "subject_label")
        subject_root = subject_root.expanduser().resolve()
        if not subject_root.is_dir():
            raise EvaluationError(f"subject_root is not a directory: {subject_root}")

        evaluation_dir = self.runtime_root / "evaluations" / spec.evaluation_id / subject_label
        events_path = evaluation_dir / "evaluation_events.jsonl"
        self._atomic_write_json(evaluation_dir / "evaluation_spec.json", spec.to_dict())
        self._append_event(
            events_path,
            {
                "event": "evaluation_started",
                "subject_label": subject_label,
                "revision": revision,
                "spec_sha256": spec.sha256,
                "created_at": self.clock(),
            },
        )

        command_results: list[CommandResult] = []
        reasons: list[str] = []
        for command in spec.commands:
            _validate_argv(command.argv)
            cwd = (subject_root / command.cwd_relative).resolve()
            if not _is_relative_to(cwd, subject_root):
                raise EvaluationError(f"{command.name}: cwd escapes subject_root")
            if not cwd.is_dir():
                raise EvaluationError(f"{command.name}: cwd does not exist: {cwd}")

            started = time.monotonic()
            source_env = dict(os.environ)
            try:
                completed = subprocess.run(
                    list(command.argv),
                    cwd=str(cwd),
                    env=sanitize_environment(base=source_env, extra=command.extra_env),
                    capture_output=True,
                    text=True,
                    timeout=command.timeout_seconds,
                    check=False,
                    shell=False,
                )
                result = CommandResult(
                    name=command.name,
                    argv=command.argv,
                    cwd=str(cwd),
                    exit_code=completed.returncode,
                    timed_out=False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    stdout_tail=_tail(completed.stdout or "", self.tail_chars, source_env),
                    stderr_tail=_tail(completed.stderr or "", self.tail_chars, source_env),
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                result = CommandResult(
                    name=command.name,
                    argv=command.argv,
                    cwd=str(cwd),
                    exit_code=None,
                    timed_out=True,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    stdout_tail=_tail(stdout, self.tail_chars, source_env),
                    stderr_tail=_tail(stderr, self.tail_chars, source_env),
                )
            command_results.append(result)
            self._append_event(
                events_path,
                {
                    "event": "command_finished",
                    "command": result.to_dict(),
                    "created_at": self.clock(),
                },
            )
            if result.timed_out:
                reasons.append(f"command timed out: {result.name}")
                break
            if result.exit_code != 0:
                reasons.append(f"command failed: {result.name} exit={result.exit_code}")
                break

        missing_artifacts: list[str] = []
        for relative in spec.required_artifacts:
            lexical_artifact = subject_root / relative
            if lexical_artifact.is_symlink():
                missing_artifacts.append(relative)
                continue
            artifact = lexical_artifact.resolve()
            if not _is_relative_to(artifact, subject_root):
                missing_artifacts.append(relative)
                continue
            if not artifact.is_file():
                missing_artifacts.append(relative)
        if missing_artifacts:
            reasons.append("missing required artifacts: " + ", ".join(sorted(missing_artifacts)))

        metrics: tuple[MetricResult, ...] = ()
        if not reasons:
            try:
                metrics = self._load_metrics(subject_root, spec)
            except EvaluationError as exc:
                reasons.append(str(exc))

        report = EvaluationReport(
            subject_label=subject_label,
            revision=revision,
            spec_sha256=spec.sha256,
            command_results=tuple(command_results),
            metrics=metrics,
            required_artifacts_ok=not missing_artifacts,
            missing_artifacts=tuple(sorted(missing_artifacts)),
            passed=not reasons,
            reasons=tuple(reasons),
            created_at=self.clock(),
        ).with_integrity()

        self._atomic_write_json(evaluation_dir / f"{subject_label}_report.json", report.to_dict())
        self._append_event(
            events_path,
            {
                "event": "evaluation_finished",
                "report_sha256": report.integrity_sha256,
                "passed": report.passed,
                "created_at": self.clock(),
            },
        )
        return report
