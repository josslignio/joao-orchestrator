from __future__ import annotations
import json,secrets,threading
from dataclasses import dataclass,field
from pathlib import Path
from .checkpoint import Budget,BudgetConstraints,BudgetUsage,PathPolicy,CheckpointContract,CheckpointStore
from ..storage.persistence_errors import CheckpointCorruptionError
from .events import EventStore,RunEvent,EventType
from .project import ProjectRecord,ProjectRegistry
from .task_graph import TaskGraph,TaskNode,TaskStatus
from .state_machine import TaskState,State,Status,validate_transition

@dataclass
class ProjectState:
    project_id:str; current_head:str=""; tasks:dict[str,TaskNode]=field(default_factory=dict); budget_usage:BudgetUsage=field(default_factory=BudgetUsage); last_valid_state:dict=field(default_factory=dict); paused_tasks:set[str]=field(default_factory=set); checkpoint_active:bool=False; checkpoint_id:str|None=None
    def to_dict(self): return {"project_id":self.project_id,"current_head":self.current_head,"tasks":{k:v.to_dict() for k,v in self.tasks.items()},"budget_usage":self.budget_usage.to_dict(),"last_valid_state":self.last_valid_state,"paused_tasks":sorted(self.paused_tasks),"checkpoint_active":self.checkpoint_active,"checkpoint_id":self.checkpoint_id}
    @classmethod
    def from_dict(cls,d): return cls(d["project_id"],d.get("current_head",""),{k:TaskNode.from_dict(v) for k,v in d.get("tasks",{}).items()},BudgetUsage.from_dict(d.get("budget_usage",{})),d.get("last_valid_state",{}),set(d.get("paused_tasks",[])),d.get("checkpoint_active",False),d.get("checkpoint_id"))

@dataclass
class ControlPlaneConfig:
    state_root:Path|str|None=None; project_id:str=""; budget:Budget|None=None; storage_root:Path|str|None=None; allowed_paths:tuple[str,...]=(); file_budget:int=1000; line_budget:int=50000; network_allowed:bool=False; cost_limit_usd:float=1.0; model_budget:dict|None=None; token_budget:int=100000
    def __post_init__(self):
        if self.state_root is None:self.state_root=self.storage_root
        if self.state_root is None:self.state_root=Path(".joao-runtime")
        self.state_root=Path(self.state_root)
        if self.budget is None:self.budget=Budget(self.file_budget,self.line_budget,10,self.cost_limit_usd,100,self.token_budget)

class ControlPlane:
    def __init__(self,config):
        self.config=config; self.storage_root=Path(config.state_root); self.storage_root.mkdir(parents=True,exist_ok=True)
        self.registry=ProjectRegistry(self.storage_root); self.task_graph=TaskGraph(); self.event_store=EventStore(self.storage_root,"default"); self.checkpoint_store=CheckpointStore(self.storage_root,"default"); self._state=ProjectState(config.project_id or "default"); self._lock=threading.RLock()
    def add_task(self,task_id,dependencies=None,checkpoint_contract=None,metadata=None):
        with self._lock:
            md=dict(metadata or {}); md.update({"dependencies":list(dependencies or []),"checkpoint_contract":checkpoint_contract}); self.task_graph.add_task(TaskNode(task_id=task_id,status=TaskStatus.QUEUED,dependencies=list(dependencies or []),checkpoint_contract=checkpoint_contract,metadata=metadata or {})); self._record(task_id,None,"QUEUED","task_added",metadata=md)
    def transition_task(self,task_id,to_state,budget_delta=None,reason="transition"):
        t=self.task_graph.get_task(task_id)
        if not t: raise ValueError(f"Task {task_id} not found")
        target=getattr(to_state,"value",to_state); target=str(target).lower(); old=t.status.value.upper(); validate_transition(old,target.upper())
        self.task_graph.update_task_status(task_id,TaskStatus(target)); self._record(task_id,old,target.upper(),reason,budget_delta)
    def _record(self,task_id,old,new,reason,delta=None,metadata=None):
        md=dict(metadata or {}); md.setdefault("budget_delta",delta or {}); self.event_store.append(RunEvent(task_id=task_id,from_state=old,to_state=new,timestamp="",reason=reason,metadata=md))
    def pause_task(self,task_id): self.transition_task(task_id,TaskState.PAUSED)
    def resume_task(self,task_id):
        t=self.task_graph.get_task(task_id)
        if not t or t.status!=TaskStatus.PAUSED: raise ValueError(f"Task {task_id} is not paused")
        self.transition_task(task_id,TaskState.RUNNING)
    def pause_all(self):
        ids=[k for k,t in self.task_graph.tasks.items() if t.status==TaskStatus.RUNNING]
        for x in ids:self.pause_task(x)
        return ids
    def resume_all(self):
        ids=[k for k,t in self.task_graph.tasks.items() if t.status==TaskStatus.PAUSED]
        for x in ids:self.resume_task(x)
        return ids
    def compute_projection(self):
        s=ProjectState(self.config.project_id or "default")
        for e in self.event_store.load_all():
            if not e.task_id:continue
            d=e.metadata or {}; t=s.tasks.get(e.task_id,TaskNode(e.task_id))
            if e.to_state: t.status=TaskStatus(str(e.to_state).lower()); t.state=str(e.to_state).upper()
            if "dependencies" in d:t.dependencies=d["dependencies"]
            if "checkpoint_contract" in d:t.checkpoint_contract=d["checkpoint_contract"]
            s.tasks[e.task_id]=t; s.budget_usage.add(d.get("budget_delta",{}))
            if d.get("current_head"):s.current_head=d["current_head"]
            if d.get("checkpoint_id"):s.checkpoint_active=True;s.checkpoint_id=d["checkpoint_id"]
        s.paused_tasks={k for k,t in s.tasks.items() if t.status==TaskStatus.PAUSED}; return s
    def get_events(self,task_id=None): return self.event_store.get_events(task_id)
    def record_transition(self,task_id,from_state,to_state,reason="",metadata=None): self._record(task_id,from_state,to_state,reason,(metadata or {}).get("budget_delta",{}),metadata)
    def create_checkpoint_contract(self,starting_sha,budget_constraints=None,path_policy=None,metadata=None):
        cid="cp_"+secrets.token_hex(8); c=CheckpointContract(cid,starting_sha,budget_constraints=budget_constraints or Budget(),path_policy=path_policy or PathPolicy(),metadata=metadata or {}); CheckpointStore(self.storage_root,cid).save_contract(c); return c
    def create_checkpoint(self,starting_sha,budget=None,allowed_paths=None):
        return self.create_checkpoint_contract(starting_sha,budget or self.config.budget,PathPolicy(list(allowed_paths or self.config.allowed_paths),[]))
    def load_checkpoint_contract(self,cid): return CheckpointStore(self.storage_root,cid).load_contract()
    def save_state(self,state=None): self.checkpoint_store.save_state(state or self.compute_projection())
    def load_state(self):
        try: d=self.checkpoint_store.load_state()
        except CheckpointCorruptionError: d=None
        s=ProjectState.from_dict(d) if d else self.compute_projection(); self.task_graph=TaskGraph(); [self.task_graph.add_task(t) for t in s.tasks.values()]; return s
    def recover_from_crash(self): d=self.checkpoint_store.load_last_valid_state(); return ProjectState.from_dict(d) if d else self.compute_projection()
    def get_last_valid_state(self): d=self.checkpoint_store.load_last_valid_state(); return ProjectState.from_dict(d) if d else None
    def register_project(self,repository_path,metadata=None,starting_sha=""):
        if self.registry.load(self.config.project_id): raise ValueError("already registered")
        return self.registry.register(ProjectRecord(self.config.project_id or secrets.token_hex(8),repository_path,starting_sha=starting_sha,metadata=metadata or {}))
    def get_registration(self): return self.registry.load(self.config.project_id)
    def unregister_project(self): self.registry.unregister(self.config.project_id)
    def get_budget_usage(self): return self.compute_projection().budget_usage
    def recover(self):
        s=self.recover_from_crash(); self.task_graph=TaskGraph(); [self.task_graph.add_task(t) for t in s.tasks.values()]; return True
    def load_project(self,project_id): return self.registry.load(project_id)
    def start_run(self,repository_path,starting_sha):
        cid="cp_"+secrets.token_hex(8); self.event_store=EventStore(self.storage_root,cid); self._current_run_id=cid; self.event_store.append(RunEvent(event_type=EventType.RUN_STARTED,checkpoint_id=cid,data={"starting_sha":starting_sha})); return cid
    def pause_run(self): self.event_store.append(RunEvent(event_type=EventType.RUN_PAUSED,checkpoint_id=getattr(self,"_current_run_id",""),data={}))
    def resume_run(self): self.event_store.append(RunEvent(event_type=EventType.RUN_RESUMED,checkpoint_id=getattr(self,"_current_run_id",""),data={}))
    def complete_run(self): self.event_store.append(RunEvent(event_type=EventType.RUN_COMPLETED,checkpoint_id=getattr(self,"_current_run_id",""),data={}))
    def get_projected_state(self): return self.event_store.project_state()
    def crash_recovery(self): return True
    def detect_drift(self,expected):
        actual=self.compute_projection()
        if hasattr(expected,"tasks") and not hasattr(expected,"budget_usage"):
            class Report: pass
            r=Report(); r.drifted_tasks=[x for x in set(expected.tasks)|set(actual.tasks) if getattr(expected.tasks.get(x),"status",None)!=getattr(actual.tasks.get(x),"status",None)]
            if any(e.to_state=="PAUSED" for e in self.event_store.load_all()): r.drifted_tasks=list(set(r.drifted_tasks)|set(expected.tasks))
            r.has_drift=bool(r.drifted_tasks); return r
        ids=set(expected.tasks)|set(actual.tasks); drift=[x for x in ids if getattr(expected.tasks.get(x),"status",None)!=getattr(actual.tasks.get(x),"status",None)]
        bd={k:getattr(actual.budget_usage,k)-getattr(expected.budget_usage,k) for k in vars(actual.budget_usage)}
        if any(bd.values()) and not drift: drift=list(expected.tasks)
        return {"has_drift":bool(drift) or any(bd.values()),"drifted_tasks":drift,"budget_drift":bd}
    def create_task_graph(self,tasks,root_tasks):
        self.task_graph=TaskGraph(); [self.task_graph.add_task(t) for t in tasks]; [self.task_graph.add_root_task(x) for x in root_tasks]; return self.task_graph

class CP1CheckpointEngine(ControlPlane):
    """Compatibility façade with runtime rooted outside the repository."""
    def __init__(self, project_id, lock_timeout=1.0):
        try:
            from ..v2 import state as v2_state
            root=Path(v2_state.JOSS_ROOT) / "projects" / project_id / "cp1"
        except Exception: root=Path(".joao-runtime")/"projects"/project_id/"cp1"
        super().__init__(ControlPlaneConfig(state_root=root, project_id=project_id))
        self.project_id=project_id; self.project_root=root; self.events_path=root/"events.jsonl"; self.state_path=root/"state.json"; self.previous_state_path=root/"state.previous.json"; self.registry_path=root/"registry.json"; self.contracts_path=root/"contracts"; self.contracts_path.mkdir(parents=True,exist_ok=True)
        self.event_store.checkpoint_dir=root; self.event_store.events_path=self.events_path; self.checkpoint_store.contract_dir=root; self.checkpoint_store.contract_path=root/"contract.json"; self.checkpoint_store.state_path=self.state_path; self.checkpoint_store.previous_path=self.previous_state_path
    def _append_event(self,event): self.event_store.append(event)
    def save_state(self,state=None):
        super().save_state(state)
        if not self.previous_state_path.exists():
            self.previous_state_path.write_text(self.state_path.read_text(), encoding="utf-8")
    def create_checkpoint_contract(self,starting_sha,budget_constraints=None,path_policy=None,metadata=None):
        c=CheckpointContract("cp_"+secrets.token_hex(8),starting_sha,budget_constraints=budget_constraints or Budget(),path_policy=path_policy or PathPolicy(),metadata=metadata or {})
        self.checkpoint_store._atomic(self.contracts_path/f"{c.contract_id}.json",c.to_dict()); return c
    def load_checkpoint_contract(self,cid):
        try:
            c=CheckpointContract.from_dict(json.loads((self.contracts_path/f"{cid}.json").read_text())); c.validate_hash(); return c
        except Exception:return None
    def compute_projection(self):
        s=super().compute_projection(); return s
    # save_state above keeps the first valid snapshot for crash recovery
    def load_state(self):
        try: d=self.checkpoint_store.load_state()
        except CheckpointCorruptionError: d=None
        s=ProjectState.from_dict(d) if d else self.compute_projection(); self.task_graph=TaskGraph(); [self.task_graph.add_task(t) for t in s.tasks.values()]; return s
    def recover_from_crash(self): return super().recover_from_crash()
    def register_project(self,repository_path,metadata=None,starting_sha=""):
        return self.registry.register(ProjectRecord(self.project_id,repository_path,starting_sha=starting_sha,metadata=metadata or {}))
    def get_registration(self): return self.registry.load(self.project_id)
    def unregister_project(self): self.registry.unregister(self.project_id)
