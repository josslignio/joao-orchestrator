"""Provider bridge with honest health, identity binding and fail-closed calls."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from ..providers.base import ProviderAdapter, ProviderRequest, ProviderResponse
from .models import SupervisorCall


MAX_PROVIDER_CONTENT_CHARS = 128000
MAX_PROVIDER_ERROR_CHARS = 8000


class ProviderBridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderDescriptor:
    name: str
    family: str
    model: str
    roles: tuple[str, ...]
    priority: int = 0
    enabled_by_policy: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "family": self.family,
            "model": self.model,
            "roles": list(self.roles),
            "priority": self.priority,
            "enabled_by_policy": self.enabled_by_policy,
        }


@dataclass
class ProviderHealth:
    descriptor: ProviderDescriptor
    available: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            **self.descriptor.to_dict(),
            "available": self.available,
            "reason": self.reason,
        }


@dataclass
class _Registration:
    descriptor: ProviderDescriptor
    adapter: ProviderAdapter


class ProviderBridge:
    """Single registry/dispatch boundary for all supervisor modes.

    The bridge never trusts a provider response's identity fields.  It checks
    task_id, role and provider_name against the controller-owned registration.
    """

    def __init__(self) -> None:
        self._providers: dict[str, _Registration] = {}

    def register(self, adapter: ProviderAdapter, *, family: str, model: str,
                 priority: int = 0, enabled_by_policy: bool = True) -> None:
        name = str(adapter.name).strip()
        family = str(family).strip().lower()
        if not name or not family:
            raise ValueError("provider name and family are required")
        if name in self._providers:
            raise ValueError(f"provider already registered: {name}")
        roles = tuple(str(r) for r in adapter.supported_roles)
        self._providers[name] = _Registration(
            ProviderDescriptor(
                name=name,
                family=family,
                model=str(model),
                roles=roles,
                priority=int(priority),
                enabled_by_policy=bool(enabled_by_policy),
            ),
            adapter,
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def descriptor(self, name: str) -> ProviderDescriptor:
        try:
            return self._providers[name].descriptor
        except KeyError as exc:
            raise ProviderBridgeError(f"unknown provider: {name}") from exc

    def health(self, name: str) -> ProviderHealth:
        try:
            reg = self._providers[name]
        except KeyError:
            return ProviderHealth(
                ProviderDescriptor(name, "unknown", "unknown", ()),
                False,
                "provider is not registered",
            )
        if not reg.descriptor.enabled_by_policy:
            return ProviderHealth(reg.descriptor, False, "disabled by policy")
        try:
            available = bool(reg.adapter.is_enabled())
        except Exception as exc:
            return ProviderHealth(
                reg.descriptor, False,
                f"health probe failed: {type(exc).__name__}: {exc}",
            )
        return ProviderHealth(
            reg.descriptor,
            available,
            "" if available else "adapter unavailable or unauthenticated",
        )

    def inventory(self) -> list[dict]:
        return [self.health(name).to_dict() for name in self.names()]

    def candidates(self, *, role: str, names: Iterable[str] = ()) -> list[ProviderDescriptor]:
        allowed = set(names)
        out: list[ProviderDescriptor] = []
        for name in self.names():
            if allowed and name not in allowed:
                continue
            health = self.health(name)
            desc = health.descriptor
            if health.available and role in desc.roles:
                out.append(desc)
        out.sort(key=lambda d: (-d.priority, d.name))
        return out

    def distinct_family_candidates(self, *, role: str,
                                   names: Iterable[str] = ()) -> list[ProviderDescriptor]:
        out: list[ProviderDescriptor] = []
        used: set[str] = set()
        for desc in self.candidates(role=role, names=names):
            if desc.family in used:
                continue
            used.add(desc.family)
            out.append(desc)
        return out

    def invoke(self, provider_name: str, request: ProviderRequest, *, sequence: int) -> SupervisorCall:
        try:
            reg = self._providers[provider_name]
        except KeyError as exc:
            raise ProviderBridgeError(f"unknown provider: {provider_name}") from exc
        health = self.health(provider_name)
        if not health.available:
            raise ProviderBridgeError(
                f"provider unavailable: {provider_name}: {health.reason}"
            )
        if request.role not in reg.descriptor.roles:
            raise ProviderBridgeError(
                f"provider {provider_name} does not support role {request.role}"
            )
        started = time.monotonic()
        try:
            response = reg.adapter.invoke(request)
        except Exception as exc:
            response = ProviderResponse(
                role=request.role,
                task_id=request.task_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                provider_name=provider_name,
            )
        duration = round(time.monotonic() - started, 6)
        self._validate_response(reg.descriptor, request, response)
        content = str(response.content or "")[:MAX_PROVIDER_CONTENT_CHARS]
        error = str(response.error or "")[:MAX_PROVIDER_ERROR_CHARS]
        return SupervisorCall(
            provider=provider_name,
            family=reg.descriptor.family,
            model=reg.descriptor.model,
            role=request.role,
            ok=bool(response.ok),
            content=content,
            error=error,
            duration_seconds=duration,
            sequence=sequence,
            response_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def _validate_response(desc: ProviderDescriptor, request: ProviderRequest,
                           response: ProviderResponse) -> None:
        if response.provider_name != desc.name:
            raise ProviderBridgeError(
                f"provider identity mismatch: expected {desc.name!r}, "
                f"got {response.provider_name!r}"
            )
        if response.task_id != request.task_id:
            raise ProviderBridgeError("provider response task_id mismatch")
        if response.role != request.role:
            raise ProviderBridgeError("provider response role mismatch")
