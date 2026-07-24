#!/bin/bash
set -u
[ $# -eq 3 ] || { echo "usage: $0 SPEC ACTIVATION HMAC_KEY"; exit 2; }
SPEC="$1"
ACTIVATION="$2"
KEY="$3"
REPO="${JOAO_RUNNIGHT_REPO:-$(pwd)}"
[ -d "$REPO/.git" ] || { echo "STOP_RUNNIGHT REPO_REQUIRED"; exit 20; }
[ -f "$REPO/scripts/run_with_watchdog.py" ] || {
  echo "STOP_RUNNIGHT AUDITED_WATCHDOG_MISSING"
  exit 20
}
[ -f "$REPO/scripts/run_runnight_master.py" ] || {
  echo "STOP_RUNNIGHT AUDITED_MASTER_MISSING"
  exit 20
}
SECONDS_LIMIT="$(python3 - "$SPEC" <<'PY'
import json,sys
data=json.load(open(sys.argv[1],encoding="utf-8"))
print(int(data["limits"]["hard_duration_minutes"])*60)
PY
)"
cd "$REPO" || exit 2
PYTHONPATH=src python3 scripts/run_with_watchdog.py --seconds "$SECONDS_LIMIT" -- \
  python3 scripts/run_runnight_master.py \
    --spec "$SPEC" \
    --activation "$ACTIVATION" \
    --key-file "$KEY"
