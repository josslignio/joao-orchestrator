"""Command policy engine — danger scoring, allow/deny/prompt, audit trail.

Phase C. Layers on top of the existing binary allow/deny policy in
``policy/commands.py``. Adds:
- Danger score (0-100) for a command based on heuristics.
- Three-way decision: ALLOW / DENY / PROMPT.
- Configurable rules (allowlist, denylist, prompt-list).
- Forbidden git operations enforcement (no force-push, no reset --hard,
  no clean, no destructive rebase, no branch deletion).
- Append-only audit log for every DENY and PROMPT decision.

Design principles:
- Fail closed: unknown commands DENY by default.
- No shell=True ever (enforced by the parser).
- Deterministic scoring (same command → same score).
- JSONL audit trail preserved.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..domain.events import now_iso
from ..storage.atomic import append_line
from .commands import (
    BLOCKED_BINARIES, BLOCKED_GIT_SUBCOMMANDS, ALLOWED_GIT_SUBCOMMANDS,
    _DANGEROUS_GIT_ARGS, _contains_shell_meta, _validate_nested_execution,
)


# ---------------------------------------------------------------------------
# Decision categories
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    PROMPT = "PROMPT"


# ---------------------------------------------------------------------------
# Danger scoring heuristics
# ---------------------------------------------------------------------------

# Each rule contributes points. Final score is capped at 100.
_DANGER_RULES: List[Tuple[str, re.Pattern, int]] = []

# Dangerous flags that suggest destructive intent.
_DANGER_FLAGS = {
    "--force": 30, "-f": 15,
    "--force-with-lease": 20,
    "--hard": 40,           # reset --hard
    "--delete": 25,         # branch deletion
    "-d": 10, "-D": 25,     # short-form delete / force-delete
    "--global": 20,
    "--system": 30,
    "--no-preserve-root": 80,
    "--recursive": 15, "-r": 10,
    "--exec": 25,
    "--upload-pack": 50,
    "--receive-pack": 50,
    "--config-env": 40,
    "-c": 5, "-C": 10,
}

# Git subcommands that are inherently destructive.
_GIT_DESTRUCTIVE_SUBS = {
    "push": 35, "reset": 45, "clean": 50, "checkout": 20,
    "switch": 15, "merge": 25, "rebase": 30, "am": 20,
    "apply": 15, "cherry-pick": 20, "commit": 35, "stash": 10,
    "worktree": 15, "submodule": 20, "amend": 35,
}

# Binaries that score high by default.
_HIGH_RISK_BINARIES = {
    "rm": 60, "sudo": 90, "doas": 90, "kill": 50, "killall": 60,
    "curl": 40, "wget": 40, "ssh": 50, "scp": 50,
    "pip": 35, "pip3": 35, "npm": 30, "brew": 45,
    "apt": 50, "apt-get": 50, "launchctl": 55,
}

# Network-sensitive flags.
_NETWORK_FLAGS = {
    "--upload-pack", "--receive-pack", "--exec",
}


def compute_danger_score(argv: List[str]) -> int:
    """Compute a danger score (0-100) for a command argv.

    Heuristics:
    - +HIGH_RISK_BINARIES[base] for known dangerous executables.
    - +_GIT_DESTRUCTIVE_SUBS[sub] for destructive git subcommands.
    - +_DANGER_FLAGS[flag] for dangerous flags.
    - +20 for any shell metacharacter (should never reach here in
      allowlist mode, but scores it if present).
    - +30 for network-sensitive flags.
    - +25 for inline code execution (-c, -m with unapproved module).
    """
    if not argv:
        return 0
    import os
    score = 0
    exe = argv[0]
    base = os.path.basename(exe)

    # Binary risk.
    score += _HIGH_RISK_BINARIES.get(base, 0)

    # Shell metacharacters (defense in depth).
    for arg in argv:
        if _contains_shell_meta(arg):
            score += 20
            break

    # Flag risk.
    for arg in argv[1:]:
        if arg in _DANGER_FLAGS:
            score += _DANGER_FLAGS[arg]
        if arg in _NETWORK_FLAGS:
            score += 30
        # --foo=bar style.
        if "=" in arg:
            flag_part = arg.split("=", 1)[0]
            if flag_part in _DANGER_FLAGS:
                score += _DANGER_FLAGS[flag_part]

    # Git subcommand risk.
    if base == "git" and len(argv) >= 2:
        sub = argv[1]
        score += _GIT_DESTRUCTIVE_SUBS.get(sub, 0)
        # Specific destructive patterns.
        if sub == "reset" and "--hard" in argv[2:]:
            score += 20
        if sub == "push" and ("--force" in argv[2:]
                              or "--force-with-lease" in argv[2:]):
            score += 30
        if sub == "branch" and ("--delete" in argv[2:] or "-d" in argv[2:]
                                or "-D" in argv[2:]):
            score += 20
        if sub == "rebase" and "--abort" not in argv[2:]:
            score += 10

    # Inline code execution.
    if base in {"python", "python3"} and len(argv) >= 2:
        if argv[1] in {"-c", "-"}:
            score += 40
        if argv[1] == "-m":
            score += 15

    return min(score, 100)


# ---------------------------------------------------------------------------
# Forbidden git operations (hard enforcement)
# ---------------------------------------------------------------------------

FORBIDDEN_GIT_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("force-push", re.compile(r"\bgit\s+push\b.*(--force|-f\b|--force-with-lease)")),
    ("reset-hard", re.compile(r"\bgit\s+reset\b.*--hard")),
    ("clean-force", re.compile(r"\bgit\s+clean\b.*(-[fdx]|--force)")),
    ("branch-delete", re.compile(r"\bgit\s+branch\b.*(-[dD]|--delete)")),
    ("rebase-destructive", re.compile(r"\bgit\s+rebase\b.*(--root|--onto)")),
    ("global-config-write", re.compile(r"\bgit\s+config\b.*( --global|--system)")),
    ("amend", re.compile(r"\bgit\s+commit\b.*--amend")),
    ("filter-branch", re.compile(r"\bgit\s+filter-branch\b")),
    ("reflog-expire", re.compile(r"\bgit\s+reflog\b.*expire")),
    ("gc-prune", re.compile(r"\bgit\s+gc\b.*(--prune=now|--aggressive)")),
]


def detect_forbidden_git(command_str: str) -> Optional[str]:
    """Check a command string for forbidden git operations.

    Returns the rule name if forbidden, None if allowed.
    """
    for rule_name, pattern in FORBIDDEN_GIT_PATTERNS:
        if pattern.search(command_str):
            return rule_name
    return None


# ---------------------------------------------------------------------------
# Command policy rules
# ---------------------------------------------------------------------------

@dataclass
class CommandPolicyRules:
    """Configurable command policy rules.

    - allow_patterns: commands matching these are ALLOW (denylist + allowlist).
    - deny_patterns: commands matching these are always DENY.
    - prompt_patterns: commands matching these require PROMPT (human approval).
    - max_danger_score_allow: score threshold; above this requires PROMPT.
    - forbidden_git: if True, enforce FORBIDDEN_GIT_PATTERNS as hard DENY.
    - approved_executables: executables that may run (default-deny otherwise).
    """
    allow_patterns: List[str] = field(default_factory=list)
    deny_patterns: List[str] = field(default_factory=list)
    prompt_patterns: List[str] = field(default_factory=list)
    max_danger_score_allow: int = 30
    forbidden_git: bool = True
    approved_executables: set = field(default_factory=set)
    skip_binary_policy: bool = False
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "allow_patterns": self.allow_patterns,
            "deny_patterns": self.deny_patterns,
            "prompt_patterns": self.prompt_patterns,
            "max_danger_score_allow": self.max_danger_score_allow,
            "forbidden_git": self.forbidden_git,
            "approved_executables": sorted(self.approved_executables),
            "skip_binary_policy": self.skip_binary_policy,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CommandPolicyRules":
        return cls(
            allow_patterns=list(d.get("allow_patterns", [])),
            deny_patterns=list(d.get("deny_patterns", [])),
            prompt_patterns=list(d.get("prompt_patterns", [])),
            max_danger_score_allow=int(d.get("max_danger_score_allow", 30)),
            forbidden_git=bool(d.get("forbidden_git", True)),
            approved_executables=set(d.get("approved_executables", [])),
            skip_binary_policy=bool(d.get("skip_binary_policy", False)),
        )

    @classmethod
    def default(cls) -> "CommandPolicyRules":
        """Sensible defaults: safe allowlist, destructive git forbidden."""
        return cls(
            allow_patterns=[],
            deny_patterns=[
                r"\brm\b", r"\bsudo\b", r"\bdoas\b",
                r"\bgit\s+push\b", r"\bgit\s+reset\b",
                r"\bgit\s+clean\b", r"\bgit\s+commit\b",
            ],
            prompt_patterns=[
                r"\bgit\s+merge\b", r"\bgit\s+rebase\b",
                r"\bgit\s+checkout\b",
            ],
            max_danger_score_allow=30,
            forbidden_git=True,
            approved_executables=set(),
        )


def _match_any(command_str: str, patterns: List[str]) -> bool:
    """Return True if command_str matches any regex pattern."""
    for pat in patterns:
        try:
            if re.search(pat, command_str):
                return True
        except re.error:
            continue
    return False


# ---------------------------------------------------------------------------
# Policy decision
# ---------------------------------------------------------------------------

@dataclass
class PolicyDecision:
    """The outcome of evaluating a command against the policy."""
    decision: str = Decision.DENY.value
    danger_score: int = 0
    reason: str = ""
    rule_name: Optional[str] = None
    command: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "danger_score": self.danger_score,
            "reason": self.reason,
            "rule_name": self.rule_name,
            "command": self.command,
            "timestamp": self.timestamp,
        }

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW.value


# ---------------------------------------------------------------------------
# Command policy engine
# ---------------------------------------------------------------------------

class CommandPolicyEngine:
    """Evaluates commands against configurable rules.

    Produces a three-way decision (ALLOW/DENY/PROMPT) with a danger score and
    writes DENY/PROMPT decisions to an append-only audit log.
    """

    def __init__(self, rules: Optional[CommandPolicyRules] = None,
                 audit_path: Optional[Path] = None,
                 now_fn=None):
        self.rules = rules or CommandPolicyRules.default()
        self.audit_path = Path(audit_path) if audit_path else None
        self.now_fn = now_fn or now_iso

    def _audit(self, decision: PolicyDecision) -> None:
        if self.audit_path is None:
            return
        decision.timestamp = self.now_fn()
        append_line(
            self.audit_path,
            json.dumps(decision.to_dict(), sort_keys=True))

    def evaluate(self, argv: List[str]) -> PolicyDecision:
        """Evaluate an argv against the policy. Returns a PolicyDecision."""
        if not argv:
            return PolicyDecision(
                decision=Decision.DENY.value,
                reason="empty argv",
                command="",
                danger_score=0)
        command_str = " ".join(argv)
        danger = compute_danger_score(argv)

        # 1. Forbidden git operations (hard DENY).
        if self.rules.forbidden_git:
            git_rule = detect_forbidden_git(command_str)
            if git_rule:
                d = PolicyDecision(
                    decision=Decision.DENY.value,
                    danger_score=danger,
                    reason=f"forbidden git operation: {git_rule}",
                    rule_name=git_rule,
                    command=command_str)
                self._audit(d)
                return d

        # 2. Deny patterns (hard DENY).
        if _match_any(command_str, self.rules.deny_patterns):
            d = PolicyDecision(
                decision=Decision.DENY.value,
                danger_score=danger,
                reason="matched deny pattern",
                command=command_str)
            self._audit(d)
            return d

        # 3. Existing binary allow/deny (commands.py).
        if not self.rules.skip_binary_policy:
            ok, reason = _validate_nested_execution(argv)
            if not ok:
                d = PolicyDecision(
                    decision=Decision.DENY.value,
                    danger_score=danger,
                    reason=f"binary policy: {reason}",
                    command=command_str)
                self._audit(d)
                return d

        # 4. Approved executables (default-deny if configured and missing).
        import os
        base = os.path.basename(argv[0])
        if (self.rules.approved_executables
                and base not in self.rules.approved_executables
                and argv[0] not in self.rules.approved_executables):
            d = PolicyDecision(
                decision=Decision.DENY.value,
                danger_score=danger,
                reason=f"executable not approved: {base}",
                command=command_str)
            self._audit(d)
            return d

        # 5. Prompt patterns (require human approval).
        if _match_any(command_str, self.rules.prompt_patterns):
            d = PolicyDecision(
                decision=Decision.PROMPT.value,
                danger_score=danger,
                reason="matched prompt pattern",
                command=command_str)
            self._audit(d)
            return d

        # 6. Danger score threshold.
        if danger > self.rules.max_danger_score_allow:
            d = PolicyDecision(
                decision=Decision.PROMPT.value,
                danger_score=danger,
                reason=f"danger score {danger} exceeds "
                       f"max {self.rules.max_danger_score_allow}",
                command=command_str)
            self._audit(d)
            return d

        # 7. Allow.
        return PolicyDecision(
            decision=Decision.ALLOW.value,
            danger_score=danger,
            reason="allowed",
            command=command_str)

    def evaluate_string(self, command_str: str) -> PolicyDecision:
        """Parse a command string with shlex and evaluate it.

        Raises ValueError on unparseable input. Refuses shell=True-style
        metacharacters by evaluating the parsed argv.
        """
        try:
            argv = shlex.split(command_str)
        except ValueError as exc:
            d = PolicyDecision(
                decision=Decision.DENY.value,
                danger_score=100,
                reason=f"unparseable command: {exc}",
                command=command_str)
            self._audit(d)
            return d
        return self.evaluate(argv)


# ---------------------------------------------------------------------------
# Safe shell parser (shlex-based, no shell=True)
# ---------------------------------------------------------------------------

def safe_parse(command_str: str) -> Tuple[List[str], Optional[str]]:
    """Safely parse a command string into argv.

    Returns (argv, error). On success error is None. Uses shlex.split which
    NEVER invokes a shell. Raises on shell-injection attempts is handled by
    returning an error string instead.
    """
    if not command_str or not command_str.strip():
        return [], "empty command"
    # Reject obvious shell-injection vectors up front.
    injection_patterns = [
        r"\$\(", r"\$`", r"`", r"\${", r"&&", r"\|\|",
        r";", r">\s", r"<\s", r">>\s", r"\|",
    ]
    for pat in injection_patterns:
        if re.search(pat, command_str):
            return [], f"shell metacharacter detected: {pat}"
    try:
        argv = shlex.split(command_str)
    except ValueError as exc:
        return [], f"parse error: {exc}"
    if not argv:
        return [], "empty argv after parse"
    return argv, None
