from __future__ import annotations
import hashlib,json
from dataclasses import dataclass,field
from enum import Enum
from typing import Any

class TaskStatus(str,Enum): QUEUED="queued"; PENDING="pending"; RUNNING="running"; PAUSED="paused"; COMPLETED="completed"; FAILED="failed"; BLOCKED="blocked"
@dataclass
class TaskNode:
    task_id: str
    status: TaskStatus = TaskStatus.QUEUED
    dependencies: list[str] = field(default_factory=list)
    task_type: str = ""
    parameters: dict[str,Any] = field(default_factory=dict)
    estimated_cost_usd: float = 0.0
    state: Any = None
    checkpoint_contract: str | None = None
    metadata: dict[str,Any] = field(default_factory=dict)
    def __post_init__(self):
        if self.state is not None:
            self.status = TaskStatus(str(getattr(self.state,"value",self.state)).lower())
        self.state = {"queued":"QUEUED","pending":"PENDING","running":"RUNNING","paused":"PAUSED","completed":"COMPLETED","failed":"FAILED","blocked":"BLOCKED"}[self.status.value]
    def to_dict(self):
        return {"task_id":self.task_id,"status":self.status.value,"state":self.status.value.upper(),"dependencies":list(self.dependencies),"task_type":self.task_type,"parameters":self.parameters,"estimated_cost_usd":self.estimated_cost_usd,"checkpoint_contract":self.checkpoint_contract,"metadata":self.metadata}
    @classmethod
    def from_dict(cls,d): return cls(task_id=d["task_id"],status=TaskStatus(d.get("status",str(d.get("state","QUEUED")).lower())),dependencies=list(d.get("dependencies",[])),task_type=d.get("task_type",""),parameters=d.get("parameters",{}),estimated_cost_usd=d.get("estimated_cost_usd",0.0),checkpoint_contract=d.get("checkpoint_contract"),metadata=d.get("metadata",{}))

class TaskGraph:
    def __init__(self): self.tasks={}; self.root_tasks=tuple()
    def add_task(self,task):
        if task.task_id in self.tasks: raise ValueError(f"Task {task.task_id} already exists")
        self.tasks[task.task_id]=task
        if not task.dependencies: self.root_tasks=tuple(sorted(set(self.root_tasks)|{task.task_id}))
    def add_root_task(self,task_id):
        if task_id not in self.tasks: raise ValueError(f"Task {task_id} not found")
        self.root_tasks=tuple(sorted(set(self.root_tasks)|{task_id}))
    def get_task(self,task_id): return self.tasks.get(task_id)
    def get_dependencies(self,task_id):
        class Deps(list):
            def __contains__(self,x): return x in [getattr(y,"task_id",y) for y in self]
        return Deps([self.tasks[x] for x in self.tasks.get(task_id,TaskNode(task_id)).dependencies if x in self.tasks])
    def get_dependents(self,task_id): return {t.task_id for t in self.tasks.values() if task_id in t.dependencies}
    def update_task_status(self,task_id,status):
        if task_id not in self.tasks: raise ValueError(f"Task {task_id} not found")
        self.tasks[task_id].status=status
    def get_ready_tasks(self,completed=None):
        completed=set(completed or {x for x,t in self.tasks.items() if t.status==TaskStatus.COMPLETED})
        class ReadyTasks(set):
            def __getitem__(self, i): return TaskNode(sorted(self)[i]) if isinstance(i,int) else super().__getitem__(i)
        return ReadyTasks(x for x,t in self.tasks.items() if x not in completed and t.status==TaskStatus.QUEUED and all(d in completed for d in t.dependencies))
    def is_ready(self,task_id,completed): return task_id in self.get_ready_tasks(completed)
    def topological_order(self):
        out=[]; seen=set()
        def visit(x):
            if x in seen:return
            seen.add(x)
            for d in self.tasks.get(x,TaskNode(x)).dependencies: visit(d)
            out.append(x)
        for x in self.tasks:visit(x)
        return tuple(out)
    @property
    def graph_hash(self): return hashlib.sha256(json.dumps(self.to_dict(),sort_keys=True).encode()).hexdigest()
    def total_estimated_cost(self): return sum(x.estimated_cost_usd for x in self.tasks.values())
    def to_dict(self): return {"tasks":{k:v.to_dict() for k,v in self.tasks.items()},"root_tasks":list(self.root_tasks),"graph_hash":self.graph_hash}
    @classmethod
    def from_dict(cls,d):
        g=cls()
        for x in d.get("tasks",{}).values(): g.add_task(TaskNode.from_dict(x))
        g.root_tasks=tuple(d.get("root_tasks",[])); return g
