# JOAO local Bubble — provider-routing V1

## Start

Run the canonical CLI with the `ui` argument. It listens only on the loopback
interface and prints the URL. No scheduler, cloud service, product repository
or product runtime is started.

The browser opens a JOAO Command Center. Write the prompt as in Codex, choose
exactly one builder (GLM, Codex, or Claude), choose no review, Codex, Claude,
or both, then click Run. Quick missions always use a fresh disposable Git
sandbox and a deterministic standard-library test command.

Codex and Claude reviews are fail-closed and bound to the final diff hash. A
same-provider review is visibly labelled self-review. Both reviewers must
accept in stacked mode. A final human approval remains required in every mode.
An installed but unauthenticated provider is disabled with its real preflight
error; JOAO never silently reroutes the mission.

## States and controls

The persisted states are pending, planning, ready, building, testing,
reviewing, needs_approval, correcting, paused, blocked, failed, accepted and
stopped. Invalid transitions are rejected and recorded in events.jsonl.
Pause, resume, stop, retry, approve and reject call the same runtime methods
used by the local API. Approval is impossible before a review completes.

## Evidence and recovery

Each run is in the local JOAO state root under runs. It contains mission,
profile, memory, plan, graph, JSONL events, checkpoints, builder evidence,
test results, review evidence, changed paths, final diff, final status and a
SHA-256 manifest. Restarting the UI reads the saved run state. A stopped run
is not restarted automatically.

## Providers and safety

The builder bridges use the existing GLM adapter, the authenticated Codex CLI,
or the authenticated Claude Code subscription CLI. The runtime holds
an inter-process builder lock, checks changed paths against the profile and
stops on a violation. The default reviewer is fail-closed: a separate Codex
review proof must match the final diff hash. One repair is allowed; another
P1 result requires human approval. The core has no Job/CV or Trading logic.

## One-click macOS app

After a version has passed exact-SHA review, install the Finder application
from that immutable worktree:

```text
python3 scripts/install_macos_command_center.py --repo /absolute/accepted/worktree
```

Then open `/Applications/JOAO Command Center.app`. Startup diagnostics are
written to `~/.local/share/joao/command-center.log`.

## Rollback

Keep the accepted Git tag and its external freeze snapshot. To inspect an
older run, point the UI state root at its existing local evidence directory.
Never delete or overwrite accepted evidence; create a new run instead.
