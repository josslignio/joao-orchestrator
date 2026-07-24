#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, signal, subprocess, sys, time
def main():
    p=argparse.ArgumentParser(); p.add_argument("--seconds",type=int,required=True)
    p.add_argument("command",nargs=argparse.REMAINDER); ns=p.parse_args()
    cmd=ns.command[1:] if ns.command and ns.command[0]=="--" else ns.command
    if not cmd: raise SystemExit("missing command")
    proc=subprocess.Popen(cmd,start_new_session=True)
    deadline=time.monotonic()+ns.seconds
    while proc.poll() is None and time.monotonic()<deadline: time.sleep(0.25)
    if proc.poll() is None:
        os.killpg(os.getpgid(proc.pid),signal.SIGKILL); proc.wait()
        print("RUNNIGHT_HARD_TIMEOUT",file=sys.stderr); return 124
    return int(proc.returncode)
if __name__=="__main__": raise SystemExit(main())
