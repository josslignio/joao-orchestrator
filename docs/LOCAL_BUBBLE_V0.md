# JOAO local bubble V0

## Start

Run the canonical CLI with the ui argument. It listens only on the loopback
interface and prints the URL. No scheduler, cloud service, product repository
or product runtime is started.

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

The default builder bridge is the existing JOAO GLM adapter. The runtime holds
an inter-process builder lock, checks changed paths against the profile and
stops on a violation. The default reviewer is fail-closed: a separate Codex
review proof must match the final diff hash. One repair is allowed; another
P1 result requires human approval. The core has no Job/CV or Trading logic.

## Rollback

Keep the accepted Git tag and its external freeze snapshot. To inspect an
older run, point the UI state root at its existing local evidence directory.
Never delete or overwrite accepted evidence; create a new run instead.
