"""Secret redaction (migrated from V1.4.0 orchestrator/redaction.py, generalized).

Replaces likely-secret values with [REDACTED] while preserving diagnostic
context. Applied to all captured subprocess output and persisted artifacts.

Extends V1.4.0 with Anthropic and Z.AI key patterns (V0.2 requirement
anticipated, but the patterns are harmless to include now).

Standard library only.
"""

from __future__ import annotations

import re
from typing import List, Tuple


_REPLACEMENTS: List[Tuple[re.Pattern, str]] = [
    # GitHub personal access tokens (classic + fine-grained).
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), "[REDACTED]"),
    # OpenAI-style keys: sk-... long alphanumeric.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "[REDACTED]"),
    # Anthropic-style keys: sk-ant-...
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED]"),
    # Z.AI / GLM-style keys (sk- variants covered above; add long hex tokens).
    (re.compile(r"\b[A-Fa-f0-9]{64}\b"), "[REDACTED]"),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[A-Za-z0-9._\-]+"),
     r"\1[REDACTED]"),
    # Generic "Bearer <token>" standalone.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    # Private SSH key blocks (entire PEM block).
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL), "[REDACTED PRIVATE KEY BLOCK]"),
    # Cookie header values.
    (re.compile(r"(?i)(cookie\s*[:=]\s*).+"), r"\1[REDACTED]"),
    # Generic KEY="...", TOKEN=..., SECRET=..., PASSWORD=... assignments.
    (re.compile(
        r"\b([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Za-z0-9_]*\s*[:=]\s*[\"']?)[^\"'\s,;]+",
        re.IGNORECASE,
    ), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    """Redact likely secrets in `text`, preserving context."""
    if not text:
        return text
    out = text
    for pattern, repl in _REPLACEMENTS:
        out = pattern.sub(repl, out)
    return out


def _looks_secret(key: str, value: str) -> bool:
    ku = key.upper()
    if any(s in ku for s in ("KEY", "TOKEN", "SECRET", "PASSWORD",
                             "COOKIE", "AUTH", "CREDENTIAL")):
        return True
    if value.startswith(("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "sk-",
                         "sk-ant-")):
        return True
    return False


def redact_env(env: dict) -> dict:
    """Return a copy of an environment dict with secret-looking values
    replaced by [REDACTED]. Key names are preserved for debuggability."""
    out = {}
    for k, v in env.items():
        if v is None:
            out[k] = None
            continue
        sv = str(v)
        out[k] = redact(sv) if _looks_secret(k, sv) else sv
    return out


def scrub_env_for_subprocess(env: dict, allowlist: tuple = ()) -> dict:
    """Build a deny-by-default subprocess environment.

    Only names explicitly present in the project profile's allowlist are
    retained. Secret-looking variables are dropped even when mistakenly
    allowlisted. No ambient Git, Python, proxy, loader, or tool configuration
    leaks into validation subprocesses.
    """
    allowed = set(allowlist)
    out = {}
    for k, v in env.items():
        if k not in allowed:
            continue
        if _looks_secret(k, str(v)):
            continue
        out[k] = v
    return out
