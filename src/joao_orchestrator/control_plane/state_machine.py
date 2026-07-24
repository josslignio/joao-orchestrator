from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

class TaskState(str,Enum): QUEUED="QUEUED"; PENDING="PENDING"; RUNNING="RUNNING"; PAUSED="PAUSED"; COMPLETED="COMPLETED"; FAILED="FAILED"; BLOCKED="BLOCKED"
class Status(str,Enum): NOT_STARTED="not_started"; PENDING="pending"; ACTIVE="active"; RUNNING="running"; PAUSED="paused"; COMPLETED="completed"; FAILED="failed"; CRASHED="crashed"; TERMINATED="terminated"
class _StateValue(str):
    @property
    def value(self): return str(self)
class State:
    QUEUED=_StateValue("queued"); PENDING=_StateValue("pending"); RUNNING=_StateValue("running"); PAUSED=_StateValue("paused"); COMPLETED=_StateValue("completed"); FAILED=_StateValue("failed"); BLOCKED=_StateValue("blocked")
    def __init__(self, checkpoint_id="", status=Status.NOT_STARTED, current_task_id="", completed_tasks=(), failed_tasks=(), progress=0.0):
        self.checkpoint_id=checkpoint_id; self.status=status; self.current_task_id=current_task_id; self.completed_tasks=tuple(completed_tasks); self.failed_tasks=tuple(failed_tasks); self.progress=progress
    def to_dict(self): return {"checkpoint_id":self.checkpoint_id,"status":getattr(self.status,"value",self.status),"current_task_id":self.current_task_id,"completed_tasks":list(self.completed_tasks),"failed_tasks":list(self.failed_tasks),"progress":self.progress}
    @classmethod
    def from_dict(cls,d): return cls(d["checkpoint_id"],Status(d["status"]),d.get("current_task_id",""),tuple(d.get("completed_tasks",[])),tuple(d.get("failed_tasks",[])),d.get("progress",0.0))
VALID={"QUEUED":{"PENDING","RUNNING"},"PENDING":{"RUNNING"},"RUNNING":{"PAUSED","COMPLETED","FAILED","BLOCKED"},"PAUSED":{"RUNNING","FAILED"},"FAILED":{"PENDING","RUNNING"},"BLOCKED":{"PENDING","RUNNING"},"COMPLETED":set()}
def validate_transition(old,new):
    a=getattr(old,"value",old); b=getattr(new,"value",new); a=str(a).upper(); b=str(b).upper()
    if b not in VALID.get(a,set()): raise ValueError(f"Invalid transition from {a} to {b}")

@dataclass(frozen=True)
class RunState:
    checkpoint_id:str; status:Status; current_task_id:str; completed_tasks:tuple[str,...]; failed_tasks:tuple[str,...]; progress:float
    def to_dict(self): return {"checkpoint_id":self.checkpoint_id,"status":self.status.value,"current_task_id":self.current_task_id,"completed_tasks":list(self.completed_tasks),"failed_tasks":list(self.failed_tasks),"progress":self.progress}
    @classmethod
    def from_dict(cls,d): return cls(d["checkpoint_id"],Status(d["status"]),d.get("current_task_id",""),tuple(d.get("completed_tasks",[])),tuple(d.get("failed_tasks",[])),d.get("progress",0.0))
StateSnapshot=RunState

class StateMachine:
    def __init__(self,initial_state=None):
        self._state=initial_state; self._history=[initial_state] if initial_state else []
    @staticmethod
    def validate_transition(old,new): validate_transition(old,new)
    def transition(self,*args,**kwargs):
        if len(args)>=2:
            validate_transition(args[0],args[1]); return True
        new=args[0] if args else kwargs.get("new_status"); old=self._state.status
        if not isinstance(new,Status): new=Status(new)
        mapping={Status.NOT_STARTED:{Status.RUNNING},Status.RUNNING:{Status.PAUSED,Status.COMPLETED,Status.FAILED,Status.CRASHED},Status.PAUSED:{Status.RUNNING,Status.FAILED},Status.FAILED:{Status.RUNNING},Status.CRASHED:{Status.RUNNING}}
        if new not in mapping.get(old,set()): raise ValueError(f"Invalid transition from {old} to {new}")
        s=RunState(self._state.checkpoint_id,new,kwargs.get("task_id",self._state.current_task_id),self._state.completed_tasks,self._state.failed_tasks,kwargs.get("progress",self._state.progress)); self._state=s; self._history.append(s); return s
    @property
    def current_state(self): return self._state
    @property
    def history(self): return tuple(self._history)
    def can_transition(self,new):
        try: validate_transition(self._state.status,new); return True
        except ValueError:return False
    def reset_to(self,state): self._state=state; self._history.append(state)
    def checkpoint(self,checkpoint_id): self._state=RunState(checkpoint_id,self._state.status,self._state.current_task_id,self._state.completed_tasks,self._state.failed_tasks,self._state.progress); self._history.append(self._state); return self._state
    def get_status(self,state):
        s=getattr(state,"value",state)
        return {"queued":Status.PENDING,"pending":Status.PENDING,"running":Status.ACTIVE,"paused":Status.PAUSED,"completed":Status.TERMINATED,"failed":Status.TERMINATED}.get(str(s),Status.PENDING)
    def complete_task(self,task_id):
        self._state=RunState(self._state.checkpoint_id,self._state.status,self._state.current_task_id,tuple(sorted(set(self._state.completed_tasks)|{task_id})),self._state.failed_tasks,min(1.0,(len(self._state.completed_tasks)+1)/max(1,len(self._state.completed_tasks)+len(self._state.failed_tasks)+1))); self._history.append(self._state); return self._state
    def fail_task(self,task_id):
        self._state=RunState(self._state.checkpoint_id,self._state.status,self._state.current_task_id,self._state.completed_tasks,tuple(sorted(set(self._state.failed_tasks)|{task_id})),self._state.progress); self._history.append(self._state); return self._state
