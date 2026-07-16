"""Append-only events and deterministic projection."""
from __future__ import annotations
import json, secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

class EventType(str,Enum):
    RUN_STARTED="run_started"; RUN_PAUSED="run_paused"; RUN_RESUMED="run_resumed"; RUN_COMPLETED="run_completed"; RUN_FAILED="run_failed"; RUN_CRASHED="run_crashed"; CHECKPOINT_CREATED="checkpoint_created"; CHECKPOINT_RESTORED="checkpoint_restored"; STATE_UPDATED="state_updated"; DRIFT_DETECTED="drift_detected"; BUDGET_EXCEEDED="budget_exceeded"; STATE_CHANGE="state_change"

@dataclass
class RunEvent:
    event_id: str = ""
    event_type: EventType | None = None
    checkpoint_id: str = ""
    timestamp: str = ""
    data: dict[str,Any] = field(default_factory=dict)
    task_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    reason: str = ""
    metadata: dict[str,Any] = field(default_factory=dict)
    def __post_init__(self):
        if not self.event_id: self.event_id=secrets.token_hex(8)
        if not self.timestamp: self.timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.event_type is None: self.event_type=EventType.STATE_CHANGE
        if not self.data and self.metadata: self.data=dict(self.metadata)
        if not self.metadata and self.data: self.metadata=dict(self.data)
    def to_dict(self):
        return {"event_id":self.event_id,"event_type":self.event_type.value,"checkpoint_id":self.checkpoint_id,"timestamp":self.timestamp,"data":self.data,"task_id":self.task_id,"from_state":self.from_state,"to_state":self.to_state,"reason":self.reason,"metadata":self.metadata}
    @classmethod
    def from_dict(cls,d):
        return cls(event_id=d.get("event_id",""),event_type=EventType(d.get("event_type","state_change")),checkpoint_id=d.get("checkpoint_id",""),timestamp=d.get("timestamp",""),data=d.get("data",d.get("metadata",{})),task_id=d.get("task_id"),from_state=d.get("from_state"),to_state=d.get("to_state"),reason=d.get("reason",""),metadata=d.get("metadata",d.get("data",{})))

class EventStore:
    def __init__(self, storage_root: Path, checkpoint_id: str | None = None):
        root=Path(storage_root); self.checkpoint_id=checkpoint_id or "default"; self.checkpoint_dir=root/"checkpoints"/self.checkpoint_id; self.checkpoint_dir.mkdir(parents=True,exist_ok=True); self.events_path=self.checkpoint_dir/"events.jsonl"
    def append(self,event):
        with self.events_path.open("a",encoding="utf-8") as f: f.write(json.dumps(event.to_dict(),sort_keys=True)+"\n"); f.flush()
    def load_all(self):
        if not self.events_path.exists(): return []
        return [RunEvent.from_dict(json.loads(x)) for x in self.events_path.read_text().splitlines() if x.strip()]
    def get_events(self,task_id=None): return [e for e in self.load_all() if task_id is None or e.task_id==task_id]
    def project_state(self):
        events=self.load_all(); s={"checkpoint_id":self.checkpoint_id,"run_status":"not_started","events_count":len(events),"first_event":events[0].timestamp if events else None,"last_event":events[-1].timestamp if events else None,"checkpoints_created":0,"checkpoints_restored":0,"pause_count":0,"resume_count":0,"drift_count":0,"budget_exceeded_count":0,"last_status":None}
        for e in events:
            if e.event_type==EventType.RUN_STARTED:s["run_status"]="running"
            elif e.event_type==EventType.RUN_PAUSED:s["run_status"]="paused";s["pause_count"]+=1
            elif e.event_type==EventType.RUN_RESUMED:s["run_status"]="running";s["resume_count"]+=1
            elif e.event_type==EventType.RUN_COMPLETED:s["run_status"]="completed"
            elif e.event_type==EventType.RUN_FAILED:s["run_status"]="failed"
            elif e.event_type==EventType.RUN_CRASHED:s["run_status"]="crashed"
            elif e.event_type==EventType.CHECKPOINT_CREATED:s["checkpoints_created"]+=1
            elif e.event_type==EventType.CHECKPOINT_RESTORED:s["checkpoints_restored"]+=1
            elif e.event_type==EventType.DRIFT_DETECTED:s["drift_count"]+=1
            elif e.event_type==EventType.BUDGET_EXCEEDED:s["budget_exceeded_count"]+=1
            s["last_status"]=e.event_type.value
        return s
