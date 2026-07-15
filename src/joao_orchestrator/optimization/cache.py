"""Content-addressed execution cache (T6).

Caches deterministic results keyed by a content-addressed hash that binds:
  schema version, project, base commit, plan hash, relevant source/test hashes,
  environment fingerprint, argv, context hash, review criteria hash,
  provider/model identity (when relevant).

Cacheable: index fragments, impact graph, context packets, test selections,
successful deterministic tests, compile/import checks, C1 baselines, review
packets, memory selection.

Never cache: failed provider responses, low-confidence answers, security
decisions, auth state, credentials, mutable external data without TTL/provenance.

Rules:
  - hash verify every read (corrupt entry deleted/recomputed);
  - schema version;
  - size cap / LRU eviction;
  - no secrets (redaction enforced);
  - audit hit/miss/rejection.

Deterministic. No network. Stdlib only. Persistence outside repos.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from ..evaluation.models import sha256_json
from ..storage.atomic import append_line, atomic_write_json


SCHEMA_VERSION = 1
DEFAULT_MAX_ENTRIES = 2048
DEFAULT_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB cap

# Field names that must never appear in cached values (secrets).
_FORBIDDEN_KEY_FRAGMENTS = (
    "token", "secret", "password", "api_key", "apikey", "credential", "private_key",
)

# Secret shapes that may appear in string VALUES (not just keys).
_SECRET_VALUE_PREFIXES = (
    "-----BEGIN ",                 # PEM private key / certificate blocks
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_",   # GitHub tokens
    "AKIA", "ASIA",                # AWS access key ids
    "xox",                         # Slack tokens (xoxb/xoxp/...)
    "Bearer ",                     # bearer auth header value
    "sk-",                         # OpenAI/Stripe-style secret keys
)
_JWT_RE = None  # compiled lazily


def _looks_like_jwt(s: str) -> bool:
    """Cheap JWT-shape check: three base64url segments separated by dots."""
    global _JWT_RE
    if _JWT_RE is None:
        import re
        _JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
    return bool(_JWT_RE.match(s)) and len(s) >= 24 and s.count(".") == 2


class CacheIntegrityError(RuntimeError):
    """Raised when a cached entry fails hash verification."""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _env_fingerprint() -> str:
    """Bind the execution environment into the cache key.

    Includes the interpreter, its version, the current working directory
    (so two checkouts of the same repo do not collide), and the versions of
    common test/build tools when importable. Hashing is deterministic so
    identical environments produce identical fingerprints.
    """
    import os
    import sys
    tool_versions: dict[str, str] = {}
    for mod_name in ("pytest", "unittest"):
        try:
            mod = sys.modules.get(mod_name)
            if mod is not None and getattr(mod, "__version__", None):
                tool_versions[mod_name] = str(mod.__version__)
        except Exception:
            pass
    return sha256_json({
        "executable": sys.executable,
        "version": sys.version,
        "cwd": os.getcwd(),
        "tool_versions": tool_versions,
    })


def contains_secret(value: Any) -> bool:
    """Detect secret-like material in a value.

    Two layers:
      1. Key-name heuristic (recursive): keys containing a forbidden fragment.
      2. Value-shape heuristic (recursive): string values that look like a
         PEM block, a known provider token prefix, a bearer header, or a JWT.
    Both are deliberately conservative (false positives are acceptable here:
    a rejected cache entry is only a cache miss).
    """
    if isinstance(value, Mapping):
        for k in value.keys():
            kl = str(k).lower()
            if any(f in kl for f in _FORBIDDEN_KEY_FRAGMENTS):
                return True
        return any(contains_secret(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_secret(v) for v in value)
    if isinstance(value, str):
        if any(value.startswith(p) for p in _SECRET_VALUE_PREFIXES):
            return True
        if _looks_like_jwt(value):
            return True
    return False


# Kinds that may be cached. Security/auth/credential decisions are never
# cacheable: their non-cacheability must not depend on value-shape luck.
_CACHEABLE_KINDS = frozenset({
    "generic", "context", "context_packet", "test_selection",
    "test_command", "review", "review_verdict", "repo_index", "provider",
})


def is_cacheable_kind(kind: str) -> bool:
    """Return True only for explicitly allowlisted cache kinds."""
    return kind in _CACHEABLE_KINDS


@dataclass(frozen=True)
class CacheKey:
    """The components bound into a cache key."""
    project_id: str
    base_commit: str
    plan_hash: str
    source_hashes: tuple[tuple[str, str], ...]   # (path, sha256)
    test_hashes: tuple[tuple[str, str], ...]
    argv: tuple[str, ...]
    context_hash: str
    review_criteria_hash: str
    provider_model: str = ""
    kind: str = "generic"  # index/context/test_selection/review/...
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "project_id": self.project_id,
            "base_commit": self.base_commit,
            "plan_hash": self.plan_hash,
            "source_hashes": [list(h) for h in self.source_hashes],
            "test_hashes": [list(h) for h in self.test_hashes],
            "argv": list(self.argv),
            "context_hash": self.context_hash,
            "review_criteria_hash": self.review_criteria_hash,
            "provider_model": self.provider_model,
            "env_fingerprint": _env_fingerprint(),
        }

    @property
    def digest(self) -> str:
        """The content-addressed key digest (sha256 of bound components)."""
        return sha256_json(self.to_dict())


@dataclass(frozen=True)
class CacheEntry:
    """One cached result.

    Integrity follows the canonical evaluation/models.py pattern: the
    ``integrity_sha256`` is computed over ``unsigned_dict()`` (which EXCLUDES
    the integrity field itself), so it protects value AND metadata (kind,
    key_digest, schema_version, sizes). Mutating any field invalidates it.
    """
    key_digest: str
    kind: str
    value: Any
    integrity_sha256: str   # hash over unsigned_dict (value + metadata)
    created_at: str
    last_accessed_at: str
    size_bytes: int
    schema_version: int = SCHEMA_VERSION

    def unsigned_dict(self) -> dict[str, Any]:
        # integrity_sha256 is the integrity field; excluded from the signed
        # payload (same pattern as EvaluationReport / KeepDecision).
        return {
            "schema_version": self.schema_version,
            "key_digest": self.key_digest,
            "kind": self.kind,
            "value": self.value,
            "created_at": self.created_at,
            "last_accessed_at": self.last_accessed_at,
            "size_bytes": self.size_bytes,
        }

    def with_integrity(self) -> "CacheEntry":
        return replace(self, integrity_sha256=sha256_json(self.unsigned_dict()))

    def verify_integrity(self) -> bool:
        return bool(self.integrity_sha256) and self.integrity_sha256 == sha256_json(
            self.unsigned_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.unsigned_dict()
        payload["integrity_sha256"] = self.integrity_sha256
        return payload


class ContentCache:
    """Content-addressed cache with LRU eviction and integrity verification.

    Layout (under a state_root outside repos):
      <state_root>/cache/
        entries/<key_digest>.json    # one file per entry
        audit.jsonl                  # append-only hit/miss/rejection log
        lru.json                     # atomic snapshot of access order
    """

    def __init__(
        self,
        state_root: Path,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        now_fn=None,
    ):
        self.state_root = Path(state_root).resolve()
        self.dir = self.state_root / "cache"
        self.entries_dir = self.dir / "entries"
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.dir / "audit.jsonl"
        self.lru_path = self.dir / "lru.json"
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._now_fn = now_fn or _now_iso

    # -- path helpers ---------------------------------------------------- #

    def _entry_path(self, key_digest: str) -> Path:
        return self.entries_dir / f"{key_digest}.json"

    def _load_lru(self) -> list[str]:
        if not self.lru_path.is_file():
            return []
        try:
            return json.loads(self.lru_path.read_text()).get("order", [])
        except (json.JSONDecodeError, OSError):
            return []

    def _save_lru(self, order: list[str]) -> None:
        atomic_write_json(self.lru_path, {"order": order, "updated_at": self._now_fn()})

    def _audit(self, event: str, key_digest: str, kind: str, detail: str = "") -> None:
        append_line(self.audit_path, json.dumps({
            "ts": self._now_fn(), "event": event, "key_digest": key_digest,
            "kind": kind, "detail": detail,
        }, ensure_ascii=False))

    # -- core operations ------------------------------------------------- #

    def get(self, key: CacheKey) -> Optional[Any]:
        """Return the cached value, or None on miss.

        Verifies integrity on every read. Corrupt entries are deleted and
        recomputed (reported as a miss with an audit rejection).
        """
        digest = key.digest
        path = self._entry_path(digest)
        if not path.is_file():
            self._audit("miss", digest, key.kind, "not present")
            return None
        try:
            entry = self._load_entry(path)
        except (json.JSONDecodeError, OSError):
            self._delete(digest)
            self._audit("reject", digest, key.kind, "unreadable")
            return None
        if not entry.verify_integrity():
            self._delete(digest)
            self._audit("reject", digest, key.kind, "integrity failure")
            return None
        if entry.schema_version != SCHEMA_VERSION:
            self._delete(digest)
            self._audit("reject", digest, key.kind, "schema mismatch")
            return None
        # Update LRU access time.
        self._touch(digest)
        self._audit("hit", digest, key.kind)
        return entry.value

    def put(self, key: CacheKey, value: Any) -> None:
        """Cache a value. Rejects secrets and enforces size cap / LRU."""
        if not is_cacheable_kind(key.kind):
            self._audit("reject", key.digest, key.kind, "non-cacheable kind")
            raise ValueError(f"refusing to cache non-cacheable kind: {key.kind!r}")
        if contains_secret(value):
            self._audit("reject", key.digest, key.kind, "secret-like field detected")
            raise ValueError("refusing to cache value containing secret-like fields")
        entry = CacheEntry(
            key_digest=key.digest,
            kind=key.kind,
            value=value,
            integrity_sha256="",  # filled by with_integrity
            created_at=self._now_fn(),
            last_accessed_at=self._now_fn(),
            size_bytes=len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")),
        ).with_integrity()
        # Evict if over capacity.
        self._evict_for(entry.size_bytes)
        atomic_write_json(self._entry_path(key.digest), entry.to_dict())
        self._touch(key.digest)
        self._audit("store", key.digest, key.kind)

    def _load_entry(self, path: Path) -> CacheEntry:
        d = json.loads(path.read_text())
        return CacheEntry(
            key_digest=str(d["key_digest"]),
            kind=str(d["kind"]),
            value=d["value"],
            integrity_sha256=str(d.get("integrity_sha256", "")),
            created_at=str(d.get("created_at", "")),
            last_accessed_at=str(d.get("last_accessed_at", "")),
            size_bytes=int(d.get("size_bytes", 0)),
            schema_version=int(d.get("schema_version", SCHEMA_VERSION)),
        )

    def _touch(self, digest: str) -> None:
        order = self._load_lru()
        if digest in order:
            order.remove(digest)
        order.append(digest)
        self._save_lru(order)

    def _delete(self, digest: str) -> None:
        path = self._entry_path(digest)
        if path.is_file():
            path.unlink()
        order = self._load_lru()
        if digest in order:
            order.remove(digest)
            self._save_lru(order)

    def _total_size(self) -> int:
        total = 0
        for p in self.entries_dir.glob("*.json"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def _entry_count(self) -> int:
        return sum(1 for _ in self.entries_dir.glob("*.json"))

    def _evict_for(self, incoming_bytes: int) -> None:
        """Evict LRU entries until the new entry fits within caps."""
        order = self._load_lru()
        while order and (
            self._entry_count() >= self.max_entries
            or self._total_size() + incoming_bytes > self.max_bytes
        ):
            oldest = order.pop(0)
            path = self._entry_path(oldest)
            if path.is_file():
                path.unlink()
            self._audit("evict", oldest, "lru", "capacity")
        self._save_lru(order)

    # -- introspection --------------------------------------------------- #

    def stats(self) -> dict[str, Any]:
        order = self._load_lru()
        hits = misses = rejects = stores = evicts = 0
        if self.audit_path.is_file():
            for line in self.audit_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                e = rec.get("event", "")
                if e == "hit": hits += 1
                elif e == "miss": misses += 1
                elif e == "reject": rejects += 1
                elif e == "store": stores += 1
                elif e == "evict": evicts += 1
        return {
            "entries": self._entry_count(),
            "total_bytes": self._total_size(),
            "lru_depth": len(order),
            "audit": {"hits": hits, "misses": misses, "rejects": rejects,
                      "stores": stores, "evicts": evicts},
            "max_entries": self.max_entries,
            "max_bytes": self.max_bytes,
        }

    def clear(self) -> None:
        for p in self.entries_dir.glob("*.json"):
            p.unlink()
        self._save_lru([])
        self._audit("clear", "", "lru", "manual")
