"""Deterministic CP1 checkpoint contracts and atomic state storage."""
from __future__ import annotations
import hashlib, json, os, tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

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
    def __init__(self, storage_root: Path, checkpoint_id: str | None = None):
        self.storage_root=Path(storage_root); self.contract_id=checkpoint_id
        self.contract_dir=self.storage_root / "checkpoints" / checkpoint_id if checkpoint_id else self.storage_root
        self.contract_dir.mkdir(parents=True,exist_ok=True)
        self.contracts_dir=self.contract_dir
        self.contract_path=self.contract_dir/"contract.json"; self.state_path=self.contract_dir/"state.json"; self.previous_path=self.contract_dir/"state.previous.json"
    def _atomic(self,path,data):
        path.parent.mkdir(parents=True,exist_ok=True)
        fd,tmp=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent); os.close(fd)
        try:
            Path(tmp).write_text(json.dumps(data,sort_keys=True,indent=2),encoding="utf-8"); os.replace(tmp,path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    def save(self, contract): self.save_contract(contract)
    def save_contract(self, contract): self.contract_id=contract.contract_id; self.contract_dir=self.storage_root/"checkpoints"/contract.contract_id; self.contract_dir.mkdir(parents=True,exist_ok=True); self.contract_path=self.contract_dir/"contract.json"; self._atomic(self.contract_path,contract.to_dict())
    def load(self, contract_id=None):
        if contract_id is not None: return CheckpointStore(self.storage_root,contract_id).load_contract()
        return self.load_contract()
    def load_contract(self):
        try:
            if not self.contract_path.exists(): return None
            c=CheckpointContract.from_dict(json.loads(self.contract_path.read_text())); c.validate_hash(); return c
        except (OSError,ValueError,json.JSONDecodeError): return None
    def save_state(self,state):
        data=state.to_dict() if hasattr(state,"to_dict") else dict(state)
        if self.state_path.exists():
            old=self.load_state()
            if old is not None: self._atomic(self.previous_path,old)
        self._atomic(self.state_path,data)
    def load_state(self):
        try: return json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        except (OSError,json.JSONDecodeError): return None
    def load_previous_state(self):
        try: return json.loads(self.previous_path.read_text()) if self.previous_path.exists() else None
        except (OSError,json.JSONDecodeError): return None
    def load_last_valid_state(self): return self.load_state() or self.load_previous_state()
