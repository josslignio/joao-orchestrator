"""Comprehensive secret detection and redaction engine.

Phase F. Extends the existing ``redaction.py`` with:
- detect-secrets-style regex rules (data-driven, configurable).
- gitleaks rules consumed as DATA ONLY (a TOML/JSON loader that reads
  external rule files but never executes them).
- Redaction applied BEFORE persistence (via a writer wrapper).
- Explicit coverage for: OpenAI, Claude/Anthropic, Z.AI, GitHub, AWS,
  SSH keys, and generic tokens.

Design principles:
- All rules are data (regex patterns + replacements). No rule is code.
- gitleaks rules are read-only data; we never invoke gitleaks itself.
- Fail safe: when in doubt, redact (false positive is safer than leak).
- Deterministic and offline-testable.

Inspired by:
- detect-secrets (Apache-2.0): regex-based baseline detectors.
- gitleaks (MIT): rule file format for secret patterns.
Only the *patterns* (data) are adopted; no source code is copied.
See THIRD_PARTY_NOTICES.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..domain.events import now_iso
from ..storage.atomic import atomic_write_json, append_line


# ---------------------------------------------------------------------------
# Rule schema (data-driven)
# ---------------------------------------------------------------------------

@dataclass
class SecretRule:
    """A single secret-detection rule (data, not code)."""
    rule_id: str = ""
    description: str = ""
    pattern: str = ""
    replacement: str = "[REDACTED]"
    severity: str = "high"        # critical | high | medium | low
    provider: str = ""             # openai | anthropic | github | aws | ...
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "description": self.description,
            "pattern": self.pattern,
            "replacement": self.replacement,
            "severity": self.severity,
            "provider": self.provider,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SecretRule":
        return cls(
            rule_id=d.get("rule_id", ""),
            description=d.get("description", ""),
            pattern=d.get("pattern", ""),
            replacement=d.get("replacement", "[REDACTED]"),
            severity=d.get("severity", "high"),
            provider=d.get("provider", ""),
            enabled=bool(d.get("enabled", True)),
        )

    def compile(self) -> Optional["CompiledRule"]:
        """Compile this rule into a CompiledRule. Returns None on regex error."""
        if not self.pattern or not self.enabled:
            return None
        try:
            # Private-key blocks span multiple lines → DOTALL.
            flags = re.DOTALL if "PRIVATE KEY" in self.pattern else 0
            return CompiledRule(
                rule=self,
                regex=re.compile(self.pattern, flags),
            )
        except re.error:
            return None


@dataclass
class CompiledRule:
    """A compiled SecretRule."""
    rule: SecretRule
    regex: re.Pattern


# ---------------------------------------------------------------------------
# Default rule set (detect-secrets-style, provider-tagged)
# ---------------------------------------------------------------------------

DEFAULT_RULES: List[dict] = [
    # GitHub.
    {"rule_id": "github_pat_classic",
     "description": "GitHub personal access token (classic)",
     "pattern": r"ghp_[A-Za-z0-9]{36,}",
     "provider": "github", "severity": "critical"},
    {"rule_id": "github_oauth",
     "description": "GitHub OAuth token",
     "pattern": r"gho_[A-Za-z0-9]{36,}",
     "provider": "github", "severity": "critical"},
    {"rule_id": "github_fine_grained",
     "description": "GitHub fine-grained personal access token",
     "pattern": r"github_pat_[A-Za-z0-9_]{22,}",
     "provider": "github", "severity": "critical"},
    {"rule_id": "github_user_token",
     "description": "GitHub user-to-server token",
     "pattern": r"ghu_[A-Za-z0-9]{36,}",
     "provider": "github", "severity": "critical"},
    {"rule_id": "github_server_token",
     "description": "GitHub server-to-server token",
     "pattern": r"ghs_[A-Za-z0-9]{36,}",
     "provider": "github", "severity": "critical"},
    {"rule_id": "github_refresh",
     "description": "GitHub refresh token",
     "pattern": r"ghr_[A-Za-z0-9]{76,}",
     "provider": "github", "severity": "critical"},

    # OpenAI.
    {"rule_id": "openai_api_key",
     "description": "OpenAI API key",
     "pattern": r"\bsk-[A-Za-z0-9]{20,}",
     "provider": "openai", "severity": "critical"},
    {"rule_id": "openai_project_key",
     "description": "OpenAI project-scoped key",
     "pattern": r"\bsk-proj-[A-Za-z0-9_\-]{20,}",
     "provider": "openai", "severity": "critical"},

    # Anthropic / Claude.
    {"rule_id": "anthropic_api_key",
     "description": "Anthropic API key",
     "pattern": r"\bsk-ant-[A-Za-z0-9_\-]{20,}",
     "provider": "anthropic", "severity": "critical"},

    # Z.AI / GLM.
    {"rule_id": "zai_api_key_hex64",
     "description": "Z.AI 64-char hex API key",
     "pattern": r"\b[A-Fa-f0-9]{64}\b",
     "provider": "zai", "severity": "critical"},
    {"rule_id": "zai_long_token",
     "description": "Z.AI long alphanumeric token",
     "pattern": r"\bzai-[A-Za-z0-9]{32,}",
     "provider": "zai", "severity": "high"},

    # AWS.
    {"rule_id": "aws_access_key_id",
     "description": "AWS access key ID",
     "pattern": r"\bAKIA[0-9A-Z]{16}\b",
     "provider": "aws", "severity": "critical"},
    {"rule_id": "aws_secret_access_key",
     "description": "AWS secret access key (40-char base64)",
     "pattern": r"(?i)aws_secret_access_key\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{40}[\"']?",
     "provider": "aws", "severity": "critical"},
    {"rule_id": "aws_session_token",
     "description": "AWS session token",
     "pattern": r"\bASIA[0-9A-Z]{16}\b",
     "provider": "aws", "severity": "critical"},

    # SSH.
    {"rule_id": "ssh_private_key_block",
     "description": "SSH/PEM private key block",
     "pattern": r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
     "provider": "ssh", "severity": "critical"},
    {"rule_id": "ssh_rsa_pub_fingerprint",
     "description": "SSH RSA public key content",
     "pattern": r"\bssh-rsa\s+AAAA[A-Za-z0-9+/=]{100,}",
     "provider": "ssh", "severity": "medium"},

    # Generic tokens.
    {"rule_id": "bearer_token",
     "description": "Authorization Bearer token",
     "pattern": r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._\-]+",
     "provider": "generic", "severity": "high",
     "replacement": r"\1[REDACTED]"},
    {"rule_id": "bearer_standalone",
     "description": "Standalone Bearer token",
     "pattern": r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}",
     "provider": "generic", "severity": "high",
     "replacement": "Bearer [REDACTED]"},
    {"rule_id": "generic_key_assignment",
     "description": "Generic KEY=/TOKEN=/SECRET=/PASSWORD= assignment",
     "pattern": r"\b([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Za-z0-9_]*\s*[:=]\s*[\"']?)[^\"'\s,;]+",
     "provider": "generic", "severity": "high",
     "replacement": r"\1[REDACTED]"},
    {"rule_id": "cookie_header",
     "description": "Cookie header value",
     "pattern": r"(?i)(cookie\s*[:=]\s*).+",
     "provider": "generic", "severity": "medium",
     "replacement": r"\1[REDACTED]"},
    {"rule_id": "jwt_token",
     "description": "JWT (three base64 segments)",
     "pattern": r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b",
     "provider": "generic", "severity": "high"},

    # Slack (common in CI leaks).
    {"rule_id": "slack_token",
     "description": "Slack token",
     "pattern": r"\bxox[baprs]-[A-Za-z0-9\-]{10,}",
     "provider": "slack", "severity": "critical"},

    # Google.
    {"rule_id": "google_api_key",
     "description": "Google API key",
     "pattern": r"\bAIza[0-9A-Za-z_\-]{35}",
     "provider": "google", "severity": "high"},
]


def load_default_rules() -> List[SecretRule]:
    """Load the built-in default rule set."""
    return [SecretRule.from_dict(r) for r in DEFAULT_RULES]


# ---------------------------------------------------------------------------
# gitleaks rules loader (data only)
# ---------------------------------------------------------------------------

def load_gitleaks_rules(path: Path) -> List[SecretRule]:
    """Load gitleaks rules from a TOML/JSON file as DATA ONLY.

    gitleaks uses a TOML format with ``[[rules]]`` sections. We parse only
    the ``id``, ``description``, ``regex``, and ``keywords`` fields. We NEVER
    execute gitleaks or any external tool — this is pure data ingestion.

    Supports both TOML (preferred) and JSON fallback.
    """
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    rules: List[SecretRule] = []
    # Try JSON first (simpler).
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
            for entry in data.get("rules", []):
                rules.append(SecretRule(
                    rule_id=entry.get("id", ""),
                    description=entry.get("description", ""),
                    pattern=entry.get("regex", ""),
                    provider=entry.get("tags", ["generic"])[0]
                            if entry.get("tags") else "generic",
                    severity="high",
                ))
        except json.JSONDecodeError:
            pass
        return rules
    # Minimal TOML parser for the gitleaks [[rules]] format.
    # We only need: id, description, regex. This avoids a tomllib dependency
    # while supporting the common gitleaks rule structure.
    try:
        import tomllib
        data = tomllib.loads(text)
        for entry in data.get("rules", []):
            rules.append(SecretRule(
                rule_id=entry.get("id", ""),
                description=entry.get("description", ""),
                pattern=entry.get("regex", ""),
                provider=(entry.get("tags", ["generic"])[0]
                          if entry.get("tags") else "generic"),
                severity="high",
            ))
    except Exception:
        # Fallback: line-based extraction (best-effort).
        rules.extend(_parse_gitleaks_loose(text))
    return rules


def _parse_gitleaks_loose(text: str) -> List[SecretRule]:
    """Best-effort loose parser for gitleaks-style rules without tomllib."""
    rules: List[SecretRule] = []
    current: Optional[dict] = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "[[rules]]":
            if current:
                rules.append(SecretRule(
                    rule_id=current.get("id", ""),
                    description=current.get("description", ""),
                    pattern=current.get("regex", ""),
                    provider="generic", severity="high"))
            current = {}
        elif current is not None and "=" in stripped:
            key, _, val = stripped.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            current[key] = val
    if current:
        rules.append(SecretRule(
            rule_id=current.get("id", ""),
            description=current.get("description", ""),
            pattern=current.get("regex", ""),
            provider="generic", severity="high"))
    return rules


# ---------------------------------------------------------------------------
# Secrets engine
# ---------------------------------------------------------------------------

@dataclass
class RedactionResult:
    """The result of redacting a text block."""
    original_length: int = 0
    redacted_length: int = 0
    rules_matched: List[str] = field(default_factory=list)
    match_count: int = 0
    redacted: bool = False

    def to_dict(self) -> dict:
        return {
            "original_length": self.original_length,
            "redacted_length": self.redacted_length,
            "rules_matched": self.rules_matched,
            "match_count": self.match_count,
            "redacted": self.redacted,
        }


class SecretsEngine:
    """Applies a rule set to redact secrets from text.

    Usage:
        engine = SecretsEngine()
        clean, result = engine.redact(text)
    """

    def __init__(self, rules: Optional[List[SecretRule]] = None,
                 extra_rules_path: Optional[Path] = None):
        self.rules: List[SecretRule] = rules if rules is not None \
            else load_default_rules()
        # Merge in gitleaks rules as DATA ONLY.
        if extra_rules_path is not None:
            self.rules.extend(load_gitleaks_rules(extra_rules_path))
        self._compiled: List[CompiledRule] = []
        for rule in self.rules:
            compiled = rule.compile()
            if compiled is not None:
                self._compiled.append(compiled)

    def redact(self, text: str) -> Tuple[str, RedactionResult]:
        """Redact secrets from text. Returns (redacted_text, result)."""
        if not text:
            return text, RedactionResult()
        result = RedactionResult(original_length=len(text))
        out = text
        for compiled in self._compiled:
            matches = compiled.regex.findall(out)
            if matches:
                result.rules_matched.append(compiled.rule.rule_id)
                result.match_count += len(matches) if isinstance(
                    matches, list) else 1
                out = compiled.regex.sub(compiled.rule.replacement, out)
        result.redacted_length = len(out)
        result.redacted = result.match_count > 0
        return out, result

    def scan(self, text: str) -> List[Dict[str, Any]]:
        """Scan text for secrets WITHOUT redacting. Returns findings list.

        Each finding: {rule_id, provider, severity, match_preview (redacted)}.
        """
        findings: List[Dict[str, Any]] = []
        if not text:
            return findings
        for compiled in self._compiled:
            for m in compiled.regex.finditer(text):
                preview = m.group(0)
                # Truncate and redact the preview itself.
                if len(preview) > 20:
                    preview = preview[:8] + "…" + preview[-4:]
                findings.append({
                    "rule_id": compiled.rule.rule_id,
                    "provider": compiled.rule.provider,
                    "severity": compiled.rule.severity,
                    "preview": "[REDACTED:" + preview[:4] + "]",
                    "start": m.start(),
                    "end": m.end(),
                })
        return findings


# ---------------------------------------------------------------------------
# Redacting writer — redacts BEFORE persistence
# ---------------------------------------------------------------------------

class RedactingWriter:
    """Wraps a task store's artifact writers to redact before persistence.

    All text artifacts written through this wrapper are redacted before being
    written to disk. JSON artifacts have their string values redacted.
    """

    def __init__(self, task_store: Any, engine: Optional[SecretsEngine] = None,
                 audit_path: Optional[Path] = None):
        self.task_store = task_store
        self.engine = engine or SecretsEngine()
        self.audit_path = audit_path

    def _audit(self, project_id: str, task_id: str, artifact: str,
               result: RedactionResult) -> None:
        if self.audit_path is None or not result.redacted:
            return
        entry = {
            "ts": now_iso(),
            "project_id": project_id,
            "task_id": task_id,
            "artifact": artifact,
            "result": result.to_dict(),
        }
        append_line(self.audit_path, json.dumps(entry, sort_keys=True))

    def write_artifact_text(self, project_id: str, task_id: str,
                            name: str, text: str) -> RedactionResult:
        """Write a text artifact, redacting secrets before persistence."""
        redacted, result = self.engine.redact(text)
        self.task_store.write_artifact_text(
            project_id, task_id, name, redacted)
        self._audit(project_id, task_id, name, result)
        return result

    def write_artifact_json(self, project_id: str, task_id: str,
                            name: str, obj: Any) -> RedactionResult:
        """Write a JSON artifact, redacting string values before persistence.

        Serializes the object, redacts the serialized text (which catches
        secrets in both keys and values), then writes the redacted JSON.
        """
        serialized = json.dumps(obj, indent=2, sort_keys=True, default=str)
        redacted_text, result = self.engine.redact(serialized)
        self.task_store.write_artifact_text(
            project_id, task_id, name, redacted_text)
        self._audit(project_id, task_id, name, result)
        return result

    def _redact_json(self, obj: Any) -> Any:
        """Recursively redact string values in a JSON-serializable object."""
        if isinstance(obj, str):
            redacted, _ = self.engine.redact(obj)
            return redacted
        if isinstance(obj, dict):
            return {k: self._redact_json(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._redact_json(v) for v in obj]
        return obj
