from __future__ import annotations
import json,secrets
from dataclasses import dataclass,field
from datetime import datetime,timezone
from pathlib import Path

@dataclass
class ProjectRecord:
    project_id:str; repository_path:str; starting_sha:str=""; contract_hash:str=""; created_at:str=""; last_checkpoint_id:str=""; status:str="registered"; registered_at:str=""; active:bool=True; metadata:dict=field(default_factory=dict)
    def __post_init__(self):
        if not self.created_at:self.created_at=self.registered_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not self.registered_at:self.registered_at=self.created_at
    def to_dict(self): return {"project_id":self.project_id,"repository_path":self.repository_path,"starting_sha":self.starting_sha,"contract_hash":self.contract_hash,"created_at":self.created_at,"registered_at":self.registered_at,"last_checkpoint_id":self.last_checkpoint_id,"status":self.status,"active":self.active,"metadata":self.metadata}
    @classmethod
    def from_dict(cls,d): return cls(**{k:d[k] for k in ("project_id","repository_path")},starting_sha=d.get("starting_sha",""),contract_hash=d.get("contract_hash",""),created_at=d.get("created_at",""),registered_at=d.get("registered_at",""),last_checkpoint_id=d.get("last_checkpoint_id",""),status=d.get("status","registered"),active=d.get("active",True),metadata=d.get("metadata",{}))

class ProjectRegistration(ProjectRecord): pass
class ProjectRegistry:
    def __init__(self,root):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True); self.registry_path=self.root/"registry.json"; self.registry_dir=self.root/"registry"; self.registry_dir.mkdir(exist_ok=True); self.index_path=self.registry_dir/"projects.json"
    def _load(self):
        try:return json.loads(self.registry_path.read_text()) if self.registry_path.exists() else {}
        except:return {}
    def _save(self,d): self.registry_path.write_text(json.dumps(d,sort_keys=True,indent=2))
    def register(self,record=None,repository_path=None,starting_sha="",contract_hash=""):
        d=self._load()
        if isinstance(record,ProjectRecord):
            if record.project_id in d: raise ValueError("already registered")
        else: record=ProjectRecord(secrets.token_hex(8),repository_path or "",starting_sha,contract_hash)
        if record.project_id in d: raise ValueError("already registered")
        d[record.project_id]=record.to_dict(); self._save(d); return record
    def load(self,project_id):
        x=self._load().get(project_id); return ProjectRecord.from_dict(x) if x else None
    def get(self,project_id): return self.load(project_id)
    def update(self,project_id,**updates):
        x=self.load(project_id)
        if not x:return None
        for k,v in updates.items(): setattr(x,k,v)
        d=self._load(); d[project_id]=x.to_dict(); self._save(d); return x
    def unregister(self,project_id):
        d=self._load(); d.pop(project_id,None); self._save(d)
    def list_all(self): return [ProjectRecord.from_dict(x) for x in self._load().values()]
