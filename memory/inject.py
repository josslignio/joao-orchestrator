#!/usr/bin/env python3
"""B-28 Phase 3 — THE INJECTION authority.

THE single source of the `🧠 RÈGLES ACTIVES` block that gets wired into every role
prompt (planner / builder / reviewer). Nothing else may assemble this block: the runtime
calls `build_injection(...)` and prepends the returned text. This is deliberately NOT an
optional helper — the live launch path (RunRuntime) has no branch that skips it.

Role-aware: lessons are pre-filtered to those whose `applies_to` names the role (the 6
LOIS apply to all three). The reviewer additionally receives a `MODES DE DÉFAILLANCE
CONNUS` checklist so it actively hunts the known failure modes, not just reads rules.

Injected lesson text is UNTRUSTED advisory data: it may advise, never grant a permission
or be executed. This module only reads, ranks and formats — it never evaluates content.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from select_lessons import format_block, load_lessons, select_lessons  # noqa: E402

HEADER = "🧠 RÈGLES ACTIVES (mémoire JOÃO — les violer = échec)"
FAILURE_HEADER = "🛑 MODES DE DÉFAILLANCE CONNUS (à traquer activement, pas juste à lire)"
ROLES = ("planner", "builder", "reviewer")


@dataclass
class InjectionResult:
    role: str
    ids: list[str] = field(default_factory=list)
    block: str = ""
    token_estimate: int = 0

    def to_evidence(self) -> dict:
        return {"role": self.role, "injected_ids": self.ids,
                "count": len(self.ids), "token_estimate": self.token_estimate}


def _failure_checklist(lessons: list[dict]) -> str:
    lines = [f"- ☐ [{d['source_defect']}] {d['rule']}" for d in lessons]
    return FAILURE_HEADER + "\n" + "\n".join(lines)


def build_injection(role: str, *, project: str = "", mission_type: str = "",
                    tags=None, files_touched=None, max_tokens: int = 800,
                    lessons: list[dict] | None = None) -> InjectionResult:
    """Return the role-aware RÈGLES ACTIVES block for a mission context (deterministic)."""
    role = role.lower()
    if role not in ROLES:
        raise ValueError(f"unknown role: {role!r} (expected one of {ROLES})")
    pool = lessons if lessons is not None else load_lessons()
    role_pool = [d for d in pool if role in d.get("applies_to", [])] or pool
    sel = select_lessons(project=project, mission_type=mission_type, tags=tags,
                         files_touched=files_touched, max_tokens=max_tokens, lessons=role_pool)
    if not sel:
        return InjectionResult(role=role)
    body = format_block(sel)
    block = f"{HEADER}\n{body}"
    if role == "reviewer":
        block += "\n\n" + _failure_checklist(sel)
    ids = [d["id"] for d in sel]
    tokens = sum(len(d.get("rule", "").split()) + 4 for d in sel)
    return InjectionResult(role=role, ids=ids, block=block, token_estimate=tokens)


def prepend(role: str, base_text: str, **ctx) -> tuple[str, InjectionResult]:
    """Compose the block and prepend it to `base_text`; returns (augmented_text, result)."""
    result = build_injection(role, **ctx)
    if not result.block:
        return base_text, result
    return f"{result.block}\n\n---\n\n{base_text}", result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="show the injectable RÈGLES ACTIVES block for a role")
    ap.add_argument("role", choices=ROLES)
    ap.add_argument("--project", default="")
    ap.add_argument("--mission", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--files", default="")
    a = ap.parse_args()
    r = build_injection(a.role, project=a.project, mission_type=a.mission,
                        tags=[t for t in a.tags.split(",") if t],
                        files_touched=[f for f in a.files.split(",") if f])
    print(f"# role={r.role} ids={r.ids} (~{r.token_estimate} tokens)\n")
    print(r.block)
