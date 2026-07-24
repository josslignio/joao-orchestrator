#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path

from joao_orchestrator.providers.bridge_factory import build_default_bridge
from joao_orchestrator.run_night import RunNightActivation, RunNightController, load_hmac_key
from joao_orchestrator.run_night.adapters import SupervisorNightRunner
from joao_orchestrator.run_night.models import spec_from_dict
from joao_orchestrator.supervisor import SupervisorCore


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--spec",required=True); p.add_argument("--activation",required=True)
    p.add_argument("--key-file",required=True)
    ns=p.parse_args()
    spec=spec_from_dict(json.loads(Path(ns.spec).read_text()))
    activation=RunNightActivation.load(Path(ns.activation))
    key=load_hmac_key(Path(ns.key_file))
    bridge=build_default_bridge(timeout_seconds=420)
    supervisor=SupervisorCore(bridge,Path(spec.state_root)/"supervisor")
    report=RunNightController(
        spec,activation,SupervisorNightRunner(supervisor),hmac_key=key
    ).run()
    print(json.dumps({
        "run_id":report.run_id,"state":report.state,
        "stop_reason":report.stop_reason,"report_sha256":report.report_sha256,
        "tasks_awaiting_approval":report.tasks_awaiting_approval,
    },sort_keys=True))
    return 0 if report.state=="completed" else 2
if __name__=="__main__": raise SystemExit(main())
