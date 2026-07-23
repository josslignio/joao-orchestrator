"""Deterministic CP1 checkpoint contracts and atomic state storage."""
from __future__ import annotations
import fcntl, hashlib, json, os, tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..integrity.records import (
    IntegrityKeyManager, signed_checkpoint_for_payload,
    verify_signed_checkpoint_envelope,
)
from ..storage.persistence_errors import CheckpointCorruptionError, PersistenceError

CP1_SCHEMA_VERSION = "1.0"

@dataclass
class Budget:
    max_files: int = 1000
    max_lines: int = 50000
    max_network_calls: int = 10
    max_cost_usd: float = 1.0
    max_model_calls: int = 100
    max_tokens: int = 100000
    # legacy constructor aliases
    file_budget: int | None = None
    line_budget: int | None = None
    network_allowed: bool | None = None
    cost_limit_usd: float | None = None
    model_budget: dict[str, int] | None = None
    token_budget: int | None = None
    def __post_init__(self):
        if self.file_budget is not None: self.max_files = self.file_budget
        if self.line_budget is not None: self.max_lines = self.line_budget
        if self.cost_limit_usd is not None: self.max_cost_usd = self.cost_limit_usd
        if self.token_budget is not None: self.max_tokens = self.token_budget
    def to_dict(self):
        return {"max_files": self.max_files, "max_lines": self.max_lines,
                "max_network_calls": self.max_network_calls, "max_cost_usd": self.max_cost_usd,
                "max_model_calls": self.max_model_calls, "max_tokens": self.max_tokens}
    @classmethod
    def from_dict(cls, d): return cls(**{k:d[k] for k in ("max_files","max_lines","max_network_calls","max_cost_usd","max_model_calls","max_tokens") if k in d})

BudgetConstraints = Budget

@dataclass
class BudgetUsage:
    files_touched: int = 0
    lines_processed: int = 0
    network_calls: int = 0
    cost_usd: float = 0.0
    model_calls: int = 0
    tokens_used: int = 0
    def to_dict(self): return asdict(self)
    @classmethod
    def from_dict(cls, d): return cls(**{k:d.get(k,0) for k in asdict(cls()).keys()})
    def add(self, delta: Mapping[str, Any]):
        for k in asdict(self): setattr(self, k, getattr(self,k) + delta.get(k,0))

@dataclass
class PathPolicy:
    allowed_roots: list[str] = field(default_factory=list)
    denied_patterns: list[str] = field(default_factory=list)
    def to_dict(self): return {"allowed_roots": list(self.allowed_roots), "denied_patterns": list(self.denied_patterns)}
    @classmethod
    def from_dict(cls,d): return cls(list(d.get("allowed_roots",[])), list(d.get("denied_patterns",[])))

@dataclass
class CheckpointContract:
    contract_id: str
    starting_sha: str
    contract_hash: str = ""
    budget_constraints: Budget = field(default_factory=Budget)
    path_policy: PathPolicy = field(default_factory=PathPolicy)
    created_at: str = ""
    metadata: dict[str,Any] = field(default_factory=dict)
    # modular aliases
    budget: Budget | None = None
    allowed_paths: tuple[str,...] | None = None
    task_graph_hash: str = ""
    schema_version: str = CP1_SCHEMA_VERSION
    def __post_init__(self):
        if self.budget is not None: self.budget_constraints = self.budget
        if self.allowed_paths is not None and not self.path_policy.allowed_roots:
            self.path_policy = PathPolicy(list(self.allowed_paths), [])
        if not self.created_at: self.created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not self.contract_hash: self.contract_hash = self.compute_contract_hash()
    def unsigned_dict(self):
        return {"schema_version": self.schema_version, "contract_id": self.contract_id,
                "starting_sha": self.starting_sha, "budget_constraints": self.budget_constraints.to_dict(),
                "path_policy": self.path_policy.to_dict(), "metadata": self.metadata,
                "created_at": self.created_at, "task_graph_hash": self.task_graph_hash}
    def compute_contract_hash(self): return hashlib.sha256(json.dumps(self.unsigned_dict(),sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def validate_hash(self):
        if self.compute_contract_hash() != self.contract_hash: raise ValueError("Contract hash mismatch")
    def to_dict(self):
        d = self.unsigned_dict(); d["contract_hash"] = self.contract_hash
        d["budget"] = self.budget_constraints.to_dict(); d["allowed_paths"] = list(self.path_policy.allowed_roots)
        return d
    @classmethod
    def from_dict(cls,d):
        b = Budget.from_dict(d.get("budget_constraints", d.get("budget",{})))
        p = PathPolicy.from_dict(d.get("path_policy", {"allowed_roots":d.get("allowed_paths",[]) }))
        return cls(contract_id=d["contract_id"], starting_sha=d["starting_sha"], contract_hash=d.get("contract_hash",""), budget_constraints=b, path_policy=p, created_at=d.get("created_at",""), metadata=d.get("metadata",{}), task_graph_hash=d.get("task_graph_hash",""), schema_version=d.get("schema_version",CP1_SCHEMA_VERSION))

class CheckpointStore:
    """Inter-process-safe signed checkpoint storage.

    Every persisted contract/state is an HMAC-authenticated envelope. A corrupt
    current state may recover only from a separately verified previous state;
    corruption is never converted to a silent ``None`` for an existing file.
    """
    def __init__(self, storage_root: Path, checkpoint_id: str | None = None):
        self.storage_root = Path(storage_root)
        self.contract_id = checkpoint_id
        self.contract_dir = self.storage_root / "checkpoints" / checkpoint_id if checkpoint_id else self.storage_root
        self.contract_dir.mkdir(parents=True, exist_ok=True)
        self.contracts_dir = self.contract_dir
        self.contract_path = self.contract_dir / "contract.json"
        self.state_path = self.contract_dir / "state.json"
        self.previous_path = self.contract_dir / "state.previous.json"
        self.lock_path = self.contract_dir / ".checkpoint.lock"
        self.keys = IntegrityKeyManager(self.storage_root / ".integrity")
        self.key = self.keys.get_key()

    def _purpose(self, kind: str) -> str:
        return f"checkpoint:{self.contract_id or 'root'}:{kind}"

    def _locked(self):
        class _Lock:
            def __init__(inner, path): inner.path = path; inner.fh = None
            def __enter__(inner):
                inner.path.parent.mkdir(parents=True, exist_ok=True)
                inner.fh = inner.path.open("a+b")
                fcntl.flock(inner.fh.fileno(), fcntl.LOCK_EX)
                return inner
            def __exit__(inner, *_):
                assert inner.fh is not None
                fcntl.flock(inner.fh.fileno(), fcntl.LOCK_UN)
                inner.fh.close()
        return _Lock(self.lock_path)

    def _atomic(self, path: Path, data: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            raw = json.dumps(dict(data), sort_keys=True, indent=2).encode("utf-8")
            with os.fdopen(fd, "wb", closefd=True) as fh:
                fd = -1
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            dfd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(dfd)
            finally: os.close(dfd)
        except OSError as exc:
            raise PersistenceError(f"checkpoint atomic write failed for {path}: {exc}") from exc
        finally:
            if fd >= 0: os.close(fd)
            try: os.unlink(tmp)
            except FileNotFoundError: pass

    def _write_signed(self, path: Path, payload: Mapping[str, Any], kind: str) -> None:
        envelope, _ = signed_checkpoint_for_payload(
            payload, purpose=self._purpose(kind), key=self.key,
            metadata={"contract_id": self.contract_id or "", "kind": kind},
        )
        # Keep the historical top-level payload shape for readers that inspect
        # state.json directly, while authenticating the exact same payload in
        # a reserved field. The integrity field is never part of the payload.
        stored = dict(payload)
        stored["_integrity"] = envelope["integrity"]
        self._atomic(path, stored)

    def _read_signed(self, path: Path, kind: str) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict) or not isinstance(stored.get("_integrity"), dict):
                raise CheckpointCorruptionError("checkpoint is unsigned or has no integrity record")
            payload = {k: v for k, v in stored.items() if k != "_integrity"}
            envelope = {"payload": payload, "integrity": stored["_integrity"]}
            try:
                return verify_signed_checkpoint_envelope(
                    envelope, purpose=self._purpose(kind), key=self.key,
                )
            except CheckpointCorruptionError:
                # CP1 historically initialized state.previous.json by copying
                # state.json byte-for-byte. Such a file is still authentic,
                # but carries the state purpose rather than previous-state.
                if kind == "previous-state":
                    return verify_signed_checkpoint_envelope(
                        envelope, purpose=self._purpose("state"), key=self.key,
                    )
                raise
        except (OSError, json.JSONDecodeError, CheckpointCorruptionError, TypeError, ValueError) as exc:
            if isinstance(exc, CheckpointCorruptionError):
                raise
            raise CheckpointCorruptionError(f"corrupt checkpoint {path}: {exc}") from exc

    def save(self, contract):
        self.save_contract(contract)

    def save_contract(self, contract):
        self.contract_id = contract.contract_id
        self.contract_dir = self.storage_root / "checkpoints" / contract.contract_id
        self.contract_dir.mkdir(parents=True, exist_ok=True)
        self.contracts_dir = self.contract_dir
        self.contract_path = self.contract_dir / "contract.json"
        self.state_path = self.contract_dir / "state.json"
        self.previous_path = self.contract_dir / "state.previous.json"
        self.lock_path = self.contract_dir / ".checkpoint.lock"
        with self._locked():
            self._write_signed(self.contract_path, contract.to_dict(), "contract")

    def load(self, contract_id=None):
        if contract_id is not None:
            return CheckpointStore(self.storage_root, contract_id).load_contract()
        return self.load_contract()

    def load_contract(self):
        payload = self._read_signed(self.contract_path, "contract")
        if payload is None:
            return None
        try:
            contract = CheckpointContract.from_dict(payload)
            contract.validate_hash()
            return contract
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointCorruptionError(f"invalid checkpoint contract: {exc}") from exc

    def save_state(self, state):
        data = state.to_dict() if hasattr(state, "to_dict") else dict(state)
        with self._locked():
            if self.state_path.exists():
                old = self._read_signed(self.state_path, "state")
                if old is not None:
                    self._write_signed(self.previous_path, old, "previous-state")
            self._write_signed(self.state_path, data, "state")

    def load_state(self):
        return self._read_signed(self.state_path, "state")

    def load_previous_state(self):
        return self._read_signed(self.previous_path, "previous-state")

    def load_last_valid_state(self):
        try:
            current = self.load_state()
            if current is not None:
                return current
        except CheckpointCorruptionError:
            pass
        previous = self.load_previous_state()
        if previous is not None:
            return previous
        if self.state_path.exists():
            raise CheckpointCorruptionError("current checkpoint is corrupt and no valid previous state exists")
        return None
