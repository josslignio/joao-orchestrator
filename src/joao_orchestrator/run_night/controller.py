"""Fail-closed sequential controller for unattended read-only work."""
from __future__ import annotations
import hashlib, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .activation import RunNightActivation, consume_activation, verify_activation
from .adapters import NightTaskRunner
from .artifacts import ArtifactError, parse_artifact_exact
from .evidence import EvidenceError, EvidenceStore
from .fingerprint import fingerprint_tree
from .models import (
    NightTaskResult, NightTaskState, RunNightReport, RunNightSpec, RunNightState,
)


class RunNightError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _code(value: str, fallback: str) -> str:
    text = str(value or "").upper()
    return text if text and len(text) <= 96 and all(c.isalnum() or c in "_-" for c in text) else fallback


class RunNightController:
    def __init__(
        self, spec: RunNightSpec, activation: RunNightActivation,
        runner: NightTaskRunner, *, hmac_key: bytes,
        now_fn: Callable[[], str] = _now,
        monotonic_fn: Callable[[], float] = time.monotonic,
        fingerprint_fn: Callable[[Path], str] = fingerprint_tree,
    ):
        self.spec=spec; self.activation=activation; self.runner=runner
        self.hmac_key=hmac_key; self.now_fn=now_fn
        self.monotonic_fn=monotonic_fn; self.fingerprint_fn=fingerprint_fn

    def preflight(self) -> dict:
        return verify_activation(self.activation, self.spec, key=self.hmac_key)

    def run(self) -> RunNightReport:
        preflight=self.preflight()
        consume_activation(self.activation, Path(self.spec.state_root))
        store=EvidenceStore(Path(self.spec.state_root), self.spec.run_id)
        store.write_manifest({
            "schema_version":2, "run_id":self.spec.run_id,
            "spec_sha256":self.spec.spec_sha256,
            "activation_id":self.activation.activation_id,
            "authorized_sha":self.spec.authorized_sha,
            "mode":self.spec.mode, "state":RunNightState.PREFLIGHT_PASSED.value,
            "preflight":preflight,
            "tasks":[task.persisted_dict() for task in self.spec.tasks],
            "limits":self.spec.limits.to_dict(),
        })
        started=self.now_fn(); start_mono=self.monotonic_fn()
        results=[]; provider_calls=0; codex_calls=0; context_bytes=0
        failures=0; stop_reason="COMPLETED"; state=RunNightState.COMPLETED.value

        for task in self.spec.tasks:
            elapsed=(self.monotonic_fn()-start_mono)/60
            if elapsed >= self.spec.limits.max_duration_minutes:
                stop_reason="MAX_DURATION"; state=RunNightState.STOPPED.value; break
            if (store.root/"STOP").exists():
                stop_reason="MANUAL_STOP_FILE"; state=RunNightState.STOPPED.value; break
            if self.spec.stop_on_sensitive and task.sensitive:
                stop_reason="SENSITIVE_TASK"; state=RunNightState.BLOCKED.value; break
            if failures >= self.spec.limits.max_consecutive_failures:
                stop_reason="MAX_CONSECUTIVE_FAILURES"; state=RunNightState.STOPPED.value; break
            remaining=self.spec.limits.max_provider_calls-provider_calls
            if remaining <= 0:
                stop_reason="MAX_PROVIDER_CALLS"; state=RunNightState.STOPPED.value; break

            root=Path(task.execution_root).expanduser().resolve()
            before=self.fingerprint_fn(root); task_started=self.now_fn()
            execution=self.runner.execute(task,max_provider_calls=remaining)
            after=self.fingerprint_fn(root)
            mutation=before!=after
            p_calls=max(0,int(execution.provider_calls))
            c_calls=max(0,int(execution.codex_calls))
            c_bytes=max(0,int(execution.context_bytes))
            provider_calls+=p_calls; codex_calls+=c_calls; context_bytes+=c_bytes

            error=""
            if mutation:
                error="READ_ONLY_MUTATION"
            elif p_calls > min(remaining, task.max_provider_calls):
                error="PROVIDER_BUDGET_OVERRUN"
            elif codex_calls > self.spec.limits.max_codex_calls:
                error="CODEX_BUDGET_OVERRUN"
            elif context_bytes > self.spec.limits.max_context_bytes:
                error="CONTEXT_BUDGET_OVERRUN"
            elif len(execution.output.encode("utf-8")) > self.spec.limits.max_artifact_bytes:
                error="ARTIFACT_BUDGET_OVERRUN"
            elif not execution.ok:
                error=_code(execution.error_code,"TASK_FAILED")
            elif task.require_independent_review and (
                len(set(execution.provider_families)) < 2
                or execution.verdict != "ACCEPT"
            ):
                error="INDEPENDENT_REVIEW_REQUIRED"

            artifact=None; artifact_path=""; artifact_sha=hashlib.sha256(b"").hexdigest()
            if not error:
                try:
                    artifact=parse_artifact_exact(
                        execution.output,
                        expected_task_id=task.task_id,
                        expected_kind=task.artifact_kind,
                        expected_sha=self.spec.authorized_sha,
                    )
                except ArtifactError:
                    error="INVALID_ARTIFACT"
                else:
                    path=store.write_artifact(artifact)
                    artifact_path=str(path.relative_to(store.root))
                    artifact_sha=artifact.artifact_sha256

            ok=not error
            result=NightTaskResult(
                task_id=task.task_id, project_id=task.project_id,
                state=(NightTaskState.AWAITING_APPROVAL.value if ok else NightTaskState.BLOCKED.value),
                ok=ok, verdict=("ACCEPT" if ok else "BLOCK"),
                artifact_kind=task.artifact_kind, artifact_sha256=artifact_sha,
                artifact_path=artifact_path, provider_calls=p_calls,
                codex_calls=c_calls, context_bytes=c_bytes,
                provider_families=tuple(execution.provider_families),
                selected_provider=execution.selected_provider,
                needs_human=True, error_code=error,
                mutation_detected=mutation, started_at=task_started,
                finished_at=self.now_fn(),
            )
            results.append(result); store.append_event(result.to_dict())
            store.write_budget({
                "schema_version":2, "run_id":self.spec.run_id,
                "tasks_attempted":len(results), "provider_calls":provider_calls,
                "codex_calls":codex_calls, "context_bytes":context_bytes,
                "consecutive_failures":failures + (0 if ok else 1),
            })
            store.write_heartbeat({
                "schema_version":1, "run_id":self.spec.run_id,
                "last_task_id":task.task_id, "tasks_attempted":len(results),
                "at":self.now_fn(),
            })
            if not ok:
                failures+=1; stop_reason=error; state=RunNightState.BLOCKED.value; break
            failures=0

        report=RunNightReport(
            run_id=self.spec.run_id, spec_sha256=self.spec.spec_sha256,
            activation_id=self.activation.activation_id,
            authorized_sha=self.spec.authorized_sha, mode=self.spec.mode,
            state=state, stop_reason=stop_reason, started_at=started,
            finished_at=self.now_fn(), tasks_total=len(self.spec.tasks),
            tasks_attempted=len(results),
            tasks_awaiting_approval=sum(r.ok for r in results),
            tasks_blocked=sum(not r.ok for r in results),
            tasks_deferred=max(0,len(self.spec.tasks)-len(results)),
            provider_calls=provider_calls,codex_calls=codex_calls,
            context_bytes=context_bytes,results=tuple(results),
        )
        store.write_report(report.to_dict())
        if stop_reason!="COMPLETED": store.write_stop_reason(stop_reason)
        try: store.final_scan()
        except EvidenceError as exc:
            raise RunNightError(f"evidence boundary failed: {exc}") from exc
        return report
