"""Bounded multi-provider supervisor core.

Modes:
- direct: one explicitly named provider;
- auto: deterministic priority routing with bounded fallback;
- challenge: primary + independent challenger + independent judge;
- council: parallel opinions from distinct families + independent judge;
- builder_reviewer: builder output reviewed by a distinct provider family.

The supervisor never approves, promotes, merges or changes the write tier.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable, Optional

from ..providers.base import ProviderRequest
from .bridge import ProviderBridge, ProviderBridgeError, ProviderDescriptor
from .models import (
    SupervisorCall,
    SupervisorMode,
    SupervisorRequest,
    SupervisorResult,
    SupervisorStatus,
)
from .store import SupervisorStore


class SupervisorError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bounded(text: str, limit: int = 24000) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "\n[TRUNCATED]"


def _decision_prompt(kind: str, original: str, calls: Iterable[SupervisorCall]) -> str:
    payload = [
        {
            "provider": call.provider,
            "family": call.family,
            "ok": call.ok,
            "content": _bounded(call.content, 12000),
            "error": call.error,
        }
        for call in calls
    ]
    return (
        "You are JOAO's independent decision reviewer. "
        "Return ONLY one JSON object with keys verdict, winner_provider, reason. "
        "verdict must be ACCEPT or BLOCK. Never invent a provider.\n"
        f"decision_kind={kind}\n"
        f"original_request={_bounded(original, 12000)}\n"
        f"candidates={json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )


def _review_prompt(original: str, builder: SupervisorCall) -> str:
    return (
        "You are an independent JOAO reviewer. Return ONLY JSON with keys "
        "verdict and reason. verdict must be ACCEPT, FIX_REQUIRED or BLOCK. "
        "Review the candidate against the original request; do not execute it.\n"
        f"original_request={_bounded(original, 12000)}\n"
        f"builder_provider={builder.provider}\n"
        f"candidate={_bounded(builder.content, 20000)}"
    )


def _parse_json_decision(text: str, *, allowed_verdicts: set[str]) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SupervisorError(f"reviewer returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SupervisorError("reviewer decision must be an object")
    verdict = str(data.get("verdict", "")).upper()
    if verdict not in allowed_verdicts:
        raise SupervisorError(f"invalid reviewer verdict: {verdict!r}")
    reason = str(data.get("reason", "")).strip()
    if not reason:
        raise SupervisorError("reviewer reason is required")
    data["verdict"] = verdict
    data["reason"] = reason
    return data


class SupervisorCore:
    def __init__(self, bridge: ProviderBridge, state_root,
                 *, max_parallel_workers: int = 4):
        self.bridge = bridge
        self.store = SupervisorStore(state_root)
        self.max_parallel_workers = max(1, min(int(max_parallel_workers), 8))

    def health(self) -> dict:
        return {
            "schema_version": 1,
            "modes": [mode.value for mode in SupervisorMode],
            "providers": self.bridge.inventory(),
            "safety": {
                "auto_approve": False,
                "auto_promote": False,
                "auto_merge": False,
                "write_tier_control": False,
            },
        }

    def execute(self, request: SupervisorRequest) -> SupervisorResult:
        self._validate_request(request)
        result = SupervisorResult(
            run_id="sup-" + uuid.uuid4().hex[:16],
            task_id=request.task_id,
            project_id=request.project_id,
            mode=request.mode,
            status=SupervisorStatus.BLOCKED.value,
            started_at=_now(),
            prompt_sha256=hashlib.sha256(request.prompt.encode("utf-8")).hexdigest(),
        )
        try:
            mode = SupervisorMode(request.mode)
            if mode is SupervisorMode.DIRECT:
                self._direct(request, result)
            elif mode is SupervisorMode.AUTO:
                self._auto(request, result)
            elif mode is SupervisorMode.CHALLENGE:
                self._challenge(request, result)
            elif mode is SupervisorMode.COUNCIL:
                self._council(request, result)
            elif mode is SupervisorMode.BUILDER_REVIEWER:
                self._builder_reviewer(request, result)
            else:  # pragma: no cover - Enum constructor already rejects it.
                raise SupervisorError(f"unsupported mode: {request.mode}")
        except (SupervisorError, ProviderBridgeError, ValueError) as exc:
            result.status = SupervisorStatus.BLOCKED.value
            result.reason = str(exc)
            result.verdict = "BLOCK"
        finally:
            result.finished_at = _now()
            self.store.persist(result)
        return result

    @staticmethod
    def _validate_request(request: SupervisorRequest) -> None:
        if not request.task_id.strip() or not request.project_id.strip():
            raise ValueError("task_id and project_id are required")
        if not request.prompt.strip():
            raise ValueError("prompt is required")
        if request.max_provider_calls < 1 or request.max_provider_calls > 8:
            raise ValueError("max_provider_calls must be between 1 and 8")
        SupervisorMode(request.mode)

    def _provider_request(self, request: SupervisorRequest, *, role: str,
                          prompt: str) -> ProviderRequest:
        return ProviderRequest(
            role=role,
            task_id=request.task_id,
            project_id=request.project_id,
            prompt=prompt,
            worktree_path=request.worktree_path,
            capability_grant=request.capability_grant,
        )

    def _call(self, request: SupervisorRequest, result: SupervisorResult,
              provider: str, *, role: Optional[str] = None,
              prompt: Optional[str] = None) -> SupervisorCall:
        if len(result.calls) >= request.max_provider_calls:
            raise SupervisorError("provider call budget exhausted")
        call = self.bridge.invoke(
            provider,
            self._provider_request(
                request,
                role=role or request.role,
                prompt=prompt if prompt is not None else request.prompt,
            ),
            sequence=len(result.calls) + 1,
        )
        result.calls.append(call)
        return call

    def _direct(self, request: SupervisorRequest, result: SupervisorResult) -> None:
        provider = request.preferred_provider or (request.provider_names[0] if request.provider_names else "")
        if not provider:
            raise SupervisorError("direct mode requires preferred_provider")
        call = self._call(request, result, provider)
        if not call.ok:
            raise SupervisorError(f"provider failed: {provider}: {call.error}")
        result.status = SupervisorStatus.COMPLETED.value
        result.selected_provider = provider
        result.final_content = call.content
        result.verdict = "ACCEPT"
        result.reason = "direct provider completed"

    def _auto(self, request: SupervisorRequest, result: SupervisorResult) -> None:
        candidates = self.bridge.candidates(role=request.role, names=request.provider_names)
        if request.preferred_provider:
            candidates.sort(key=lambda d: (d.name != request.preferred_provider, -d.priority, d.name))
        if not candidates:
            raise SupervisorError("no available provider supports the requested role")
        result.routing = {"candidates": [d.name for d in candidates], "policy": "priority_then_bounded_fallback"}
        failures: list[str] = []
        for desc in candidates:
            if len(result.calls) >= request.max_provider_calls:
                break
            call = self._call(request, result, desc.name)
            if call.ok:
                result.status = SupervisorStatus.COMPLETED.value
                result.selected_provider = desc.name
                result.final_content = call.content
                result.verdict = "ACCEPT"
                result.reason = "auto route completed"
                return
            failures.append(f"{desc.name}: {call.error}")
        raise SupervisorError("all auto candidates failed: " + "; ".join(failures))

    def _pick_independent(self, *, role: str, names: tuple[str, ...],
                          excluded_families: set[str], preferred: Optional[str] = None) -> ProviderDescriptor:
        candidates = self.bridge.candidates(role=role, names=names)
        if preferred:
            candidates.sort(key=lambda d: (d.name != preferred, -d.priority, d.name))
        for desc in candidates:
            if desc.family not in excluded_families:
                return desc
        raise SupervisorError(f"no independent provider available for role {role}")

    def _challenge(self, request: SupervisorRequest, result: SupervisorResult) -> None:
        participants = self.bridge.distinct_family_candidates(role=request.role, names=request.provider_names)
        if request.preferred_provider:
            participants.sort(key=lambda d: (d.name != request.preferred_provider, -d.priority, d.name))
        if len(participants) < 2:
            raise SupervisorError("challenge mode requires two available provider families")
        primary, challenger = participants[:2]
        if request.max_provider_calls < 3:
            raise SupervisorError("challenge mode requires a call budget of at least three")
        first = self._call(request, result, primary.name)
        if not first.ok:
            raise SupervisorError(f"challenge primary failed: {first.error}")
        second_prompt = (
            "Challenge the following answer. Identify errors, missing evidence and a better answer.\n"
            f"ORIGINAL={_bounded(request.prompt, 12000)}\n"
            f"ANSWER={_bounded(first.content, 16000)}"
        )
        second = self._call(request, result, challenger.name, prompt=second_prompt)
        if not second.ok:
            raise SupervisorError(f"challenge challenger failed: {second.error}")
        judge = self._pick_independent(
            role="reviewer",
            names=request.provider_names,
            excluded_families={primary.family, challenger.family},
            preferred=request.judge_provider,
        )
        judge_call = self._call(
            request, result, judge.name, role="reviewer",
            prompt=_decision_prompt("challenge", request.prompt, [first, second]),
        )
        if not judge_call.ok:
            raise SupervisorError(f"challenge judge failed: {judge_call.error}")
        decision = _parse_json_decision(judge_call.content, allowed_verdicts={"ACCEPT", "BLOCK"})
        winner = str(decision.get("winner_provider", ""))
        by_name = {first.provider: first, second.provider: second}
        if decision["verdict"] != "ACCEPT" or winner not in by_name:
            result.status = SupervisorStatus.NEEDS_HUMAN.value
            result.needs_human = True
            result.verdict = "BLOCK"
            result.reason = decision["reason"]
            return
        result.status = SupervisorStatus.COMPLETED.value
        result.selected_provider = winner
        result.final_content = by_name[winner].content
        result.verdict = "ACCEPT"
        result.reason = decision["reason"]

    def _council(self, request: SupervisorRequest, result: SupervisorResult) -> None:
        available = self.bridge.distinct_family_candidates(
            role=request.role, names=request.provider_names
        )
        max_participants = min(
            len(available), request.max_provider_calls - 1, self.max_parallel_workers
        )
        participants = None
        judge = None
        # Choose the largest council that still leaves an independent reviewer
        # family. This avoids consuming every available family as a participant.
        for count in range(max_participants, 1, -1):
            subset = available[:count]
            try:
                candidate_judge = self._pick_independent(
                    role="reviewer",
                    names=request.provider_names,
                    excluded_families={d.family for d in subset},
                    preferred=request.judge_provider,
                )
            except SupervisorError:
                continue
            participants = subset
            judge = candidate_judge
            break
        if participants is None or judge is None:
            raise SupervisorError(
                "council mode requires at least two participant families plus an independent judge"
            )
        calls: list[SupervisorCall] = []
        with ThreadPoolExecutor(max_workers=len(participants), thread_name_prefix="joao-council") as pool:
            futures = {
                pool.submit(
                    self.bridge.invoke,
                    desc.name,
                    self._provider_request(request, role=request.role, prompt=request.prompt),
                    sequence=index + 1,
                ): desc
                for index, desc in enumerate(participants)
            }
            for future in as_completed(futures):
                calls.append(future.result())
        calls.sort(key=lambda c: c.sequence)
        result.calls.extend(calls)
        if not all(call.ok for call in calls):
            raise SupervisorError("one or more council participants failed")
        judge_call = self._call(
            request, result, judge.name, role="reviewer",
            prompt=_decision_prompt("council", request.prompt, calls),
        )
        if not judge_call.ok:
            raise SupervisorError(f"council judge failed: {judge_call.error}")
        decision = _parse_json_decision(judge_call.content, allowed_verdicts={"ACCEPT", "BLOCK"})
        by_name = {call.provider: call for call in calls}
        winner = str(decision.get("winner_provider", ""))
        if decision["verdict"] != "ACCEPT" or winner not in by_name:
            result.status = SupervisorStatus.NEEDS_HUMAN.value
            result.needs_human = True
            result.verdict = "BLOCK"
            result.reason = decision["reason"]
            return
        result.status = SupervisorStatus.COMPLETED.value
        result.selected_provider = winner
        result.final_content = by_name[winner].content
        result.verdict = "ACCEPT"
        result.reason = decision["reason"]

    def _builder_reviewer(self, request: SupervisorRequest, result: SupervisorResult) -> None:
        builders = self.bridge.candidates(role="coder", names=request.provider_names)
        if request.preferred_provider:
            builders.sort(key=lambda d: (d.name != request.preferred_provider, -d.priority, d.name))
        if not builders:
            raise SupervisorError("no available builder provider")
        builder = builders[0]
        reviewer = self._pick_independent(
            role="reviewer",
            names=request.provider_names,
            excluded_families={builder.family},
            preferred=request.judge_provider,
        )
        build_call = self._call(request, result, builder.name, role="coder")
        if not build_call.ok:
            raise SupervisorError(f"builder failed: {build_call.error}")
        review_call = self._call(
            request, result, reviewer.name, role="reviewer",
            prompt=_review_prompt(request.prompt, build_call),
        )
        if not review_call.ok:
            raise SupervisorError(f"reviewer failed: {review_call.error}")
        decision = _parse_json_decision(
            review_call.content,
            allowed_verdicts={"ACCEPT", "FIX_REQUIRED", "BLOCK"},
        )
        result.selected_provider = builder.name
        result.final_content = build_call.content
        result.verdict = decision["verdict"]
        result.reason = decision["reason"]
        if decision["verdict"] == "ACCEPT":
            result.status = SupervisorStatus.COMPLETED.value
        else:
            result.status = SupervisorStatus.NEEDS_HUMAN.value
            result.needs_human = True
