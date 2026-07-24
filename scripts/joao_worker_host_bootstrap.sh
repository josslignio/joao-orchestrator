#!/bin/bash
# joao-worker-host bootstrap: install/update the user-level LaunchAgent,
# load it, verify health, and support clean stop/restart/uninstall.
#
# Boss directive (2026-07-20): "After installation, the Boss-facing operation
# must be one command or one UI action only." This script IS that one
# command. It never dispatches a builder/reviewer or runs any product
# mission itself — only (un)installs and health-checks the standalone
# worker-host service. No administrator/root privileges required (a
# per-user LaunchAgent under ~/Library/LaunchAgents, loaded via
# `launchctl bootstrap gui/<uid>`).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_LABEL="com.joao.worker-host"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
PYTHON3="$(command -v python3)"
UID_NUM="$(id -u)"
LOG_DIR="$HOME/Library/Logs/joao"
HEALTH_TIMEOUT_S=10

usage() {
  echo "usage: $(basename "$0") [install|health|stop|restart|uninstall]" >&2
  echo "  install    write/refresh the LaunchAgent plist, (re)load it, verify health (default)" >&2
  echo "  health     probe the running worker-host; exit 0 healthy, 1 otherwise" >&2
  echo "  stop       unload the LaunchAgent (process stops; plist stays installed)" >&2
  echo "  restart    stop then install" >&2
  echo "  uninstall  stop the LaunchAgent and remove its plist" >&2
  exit 1
}

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON3}</string>
        <string>${REPO_ROOT}/scripts/joao_worker_host_run.py</string>
    </array>
    <key>WorkingDirectory</key><string>${REPO_ROOT}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key><false/>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>${HOME}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StandardOutPath</key><string>${LOG_DIR}/worker-host.log</string>
    <key>StandardErrorPath</key><string>${LOG_DIR}/worker-host.err.log</string>
    <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST
  # No secrets in the plist: only a python3 path, this repo's own script
  # path, and log file paths.
}

do_stop() {
  launchctl bootout "gui/${UID_NUM}/${PLIST_LABEL}" >/dev/null 2>&1 || true
}

# HMAC controller secret (Boss directive, 2026-07-21): generated once, here,
# at install time — idempotent, never rotated implicitly by a later install
# run. Stored OUTSIDE the LaunchAgent plist (never in ${PLIST_PATH}), inside
# the worker-host's own state dir, owner-only (0600 file, 0700 parent dir),
# never printed or logged by this script. Defense-in-depth against an
# accidental/misconfigured same-machine client — NOT a claim of protection
# against another process running as this same macOS user
# (WORKER_HOST_TRUST_BOUNDARY=same_macOS_user; see worker_host/hmac_auth.py).
do_ensure_hmac_secret() {
  (cd "$REPO_ROOT" && "$PYTHON3" - <<'PYEOF'
from src.joao_orchestrator.worker_host.hmac_auth import ensure_secret
from src.joao_orchestrator.worker_host.server import DEFAULT_STATE_DIR
path = ensure_secret(DEFAULT_STATE_DIR)
print(f"joao-worker-host HMAC controller secret ready: {path} (never printed, owner-only)")
PYEOF
  )
}

do_health() {
  "$PYTHON3" "$REPO_ROOT/scripts/joao_worker_host_health.py"
}

wait_healthy() {
  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT_S" ]; do
    if do_health >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done
  return 1
}

do_install() {
  write_plist
  do_ensure_hmac_secret
  do_stop
  launchctl bootstrap "gui/${UID_NUM}" "$PLIST_PATH"
  launchctl enable "gui/${UID_NUM}/${PLIST_LABEL}" || true
  if wait_healthy; then
    echo "joao-worker-host installed and healthy: ${PLIST_PATH}"
    echo "trust boundary: same_macOS_user (HMAC envelope is defense-in-depth against an accidental/misconfigured same-machine client, not isolation from another process running as this user)"
    do_health
  else
    echo "joao-worker-host installed but NOT healthy after ${HEALTH_TIMEOUT_S}s — check ${LOG_DIR}/worker-host.err.log" >&2
    exit 1
  fi
}

do_uninstall() {
  do_stop
  rm -f "$PLIST_PATH"
  echo "joao-worker-host uninstalled: ${PLIST_PATH} removed"
}

ACTION="${1:-install}"
case "$ACTION" in
  install) do_install ;;
  health) do_health ;;
  stop) do_stop; echo "joao-worker-host stopped (plist still installed)" ;;
  restart) do_stop; do_install ;;
  uninstall) do_uninstall ;;
  *) usage ;;
esac
