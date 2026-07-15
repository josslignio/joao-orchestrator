"""Structured command policy (allowlist-driven, shell=False).

Commands are defined as structured ``{executable, args}`` objects.  A command
must match one declared signature exactly (plus explicitly enumerated trailing
arguments).  Merely declaring an executable never grants arbitrary arguments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Tuple


BLOCKED_BINARIES = {
    "rm", "sudo", "doas", "launchctl", "curl", "wget", "ssh", "scp",
    "sftp", "rsync", "ftp", "nc", "netcat", "telnet", "pip", "pip3",
    "easy_install", "npm", "npx", "pnpm", "yarn", "brew", "apt", "apt-get",
    "dnf", "yum", "pacman", "sh", "bash", "zsh", "dash", "fish", "eval",
    "source", "exec", "kill", "killall", "env", "xargs", "nice", "nohup",
    "timeout", "open", "osascript",
}

BLOCKED_GIT_SUBCOMMANDS = {
    "commit", "push", "reset", "clean", "checkout", "switch", "merge",
    "rebase", "tag", "remote", "fetch", "pull", "clone", "init", "mv",
    "rm", "stash", "worktree", "submodule", "bisect", "cherry-pick",
    "archive", "am", "apply", "send-email", "credential",
}

ALLOWED_GIT_SUBCOMMANDS = {"diff", "status", "log", "show", "ls-files", "rev-parse"}

_DANGEROUS_GIT_ARGS = {
    "--ext-diff", "--textconv", "--no-index", "--exec-path", "--config-env",
    "--upload-pack", "--receive-pack",
}

_SAFE_PYTHON_MODULES = {"compileall", "py_compile", "unittest"}
_BLOCKED_PYTHON_SCRIPTS = {"pip", "pip3", "easy_install"}

_SHELL_META = set('|><&;`\n\r')
_SUBSTITUTION = ("$(", "${", "$`")


@dataclass
class CommandDefinition:
    """A structured command entry from a validation profile."""
    executable: str
    args: List[str] = field(default_factory=list)
    profile: str = "default"
    timeout: int = 60
    extra_args_allowed: List[str] = field(default_factory=list)
    args_extra: List[str] = field(default_factory=list)
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict, resolve: callable = None) -> "CommandDefinition":
        exe = d.get("executable", "")
        if resolve and exe in ("$PYTHON", "<python>", "python"):
            exe = resolve("python")
        elif resolve and exe in ("$GIT", "<git>", "git"):
            exe = resolve("git")
        return cls(
            executable=str(exe),
            args=[str(v) for v in d.get("args", [])],
            profile=str(d.get("profile", "default")),
            timeout=int(d.get("timeout", 60)),
            extra_args_allowed=[str(v) for v in d.get("extra_args_allowed", [])],
            args_extra=[str(v) for v in d.get("args_extra", [])],
            description=str(d.get("description", "")),
        )


def _contains_shell_meta(arg: str) -> bool:
    if not arg:
        return False
    if any(ch in _SHELL_META for ch in arg):
        return True
    return any(tok in arg for tok in _SUBSTITUTION)


def _matches_signature(argv: List[str], signature: Tuple[str, List[str], List[str]]) -> bool:
    sig_exe, sig_args, extra_allowed = signature
    if argv[0] != sig_exe:
        return False
    rest = argv[1:]
    if len(rest) < len(sig_args) or rest[:len(sig_args)] != sig_args:
        return False
    trailing = rest[len(sig_args):]
    allowed = set(extra_allowed)
    return all(token in allowed for token in trailing)


def _validate_nested_execution(argv: List[str]) -> Tuple[bool, str]:
    exe = argv[0]
    base = os.path.basename(exe)

    if base in BLOCKED_BINARIES:
        return False, f"blocked binary: {base}"

    is_python = base in {"python", "python3"} or base.startswith("python3.")
    if is_python and len(argv) >= 2:
        if argv[1] in {"-c", "-"}:
            return False, "inline/stdin Python execution is forbidden"
        if argv[1] == "-m":
            if len(argv) < 3:
                return False, "python -m requires a module"
            module = argv[2].split(".", 1)[0]
            if module not in _SAFE_PYTHON_MODULES:
                return False, f"python module not allowlisted: {module}"
        else:
            script_name = os.path.basename(argv[1]).lower()
            stem = script_name[:-3] if script_name.endswith(".py") else script_name
            if stem in _BLOCKED_PYTHON_SCRIPTS:
                return False, f"package installer invocation forbidden: {script_name}"

    if base == "git":
        if len(argv) < 2:
            return False, "git requires a subcommand"
        if argv[1].startswith("-"):
            return False, "git global options are forbidden"
        sub = argv[1]
        if sub in BLOCKED_GIT_SUBCOMMANDS:
            return False, f"blocked git subcommand: git {sub}"
        if sub not in ALLOWED_GIT_SUBCOMMANDS:
            return False, f"git subcommand not allowlisted: git {sub}"
        for arg in argv[2:]:
            if arg in _DANGEROUS_GIT_ARGS or arg.startswith("--ext-diff="):
                return False, f"dangerous git option: {arg}"
            if arg in {"-c", "-C"} or arg.startswith("--config-env="):
                return False, f"git configuration/path override forbidden: {arg}"
        if sub == "diff":
            required = {"--no-ext-diff", "--no-textconv"}
            missing = required.difference(argv[2:])
            if missing:
                return False, "git diff must disable external diff and textconv"
    elif not is_python:
        return False, f"unsupported executable class: {base}"

    return True, "ok"


def validate_argv(
    argv: List[str],
    approved_executables: set,
    allowlist_signatures: List[Tuple[str, List[str], List[str]]],
) -> Tuple[bool, str]:
    """Validate a raw argv against the command policy.

    The executable must be approved *and* the complete argv must match one
    declared signature.  This prevents wrapper, package-installation, network,
    and arbitrary Git-argument bypasses through a modified profile.
    """
    if not argv or not isinstance(argv, list):
        return False, "empty or non-list argv"
    if not all(isinstance(a, str) for a in argv):
        return False, "argv contains non-string element"
    for arg in argv:
        if _contains_shell_meta(arg):
            return False, f"shell metacharacter / substitution in arg: {arg!r}"

    exe = argv[0]
    ok, reason = _validate_nested_execution(argv)
    if not ok:
        return ok, reason
    if exe not in approved_executables:
        return False, f"executable not allowlisted: {exe}"

    if not any(_matches_signature(argv, sig) for sig in allowlist_signatures):
        return False, f"argv not in structured allowlist: {argv}"
    return True, "ok"
