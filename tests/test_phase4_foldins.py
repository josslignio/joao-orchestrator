"""PHASE 4 — bounded audit fold-ins: B-37 ledger→brain sync, B-24 Claude tiering."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MEM = Path(__file__).resolve().parents[1] / "memory"
sys.path.insert(0, str(MEM))
import ledger_sync  # noqa: E402
import import_ledger  # noqa: E402

from joao_orchestrator.providers.cascade_runtime import CascadeBuilder  # noqa: E402
from joao_orchestrator.providers.cascade import Tier  # noqa: E402

LEDGER = """# ledger
| ID | Produit | Symptôme | Cause racine | Leçon | Statut |
|---|---|---|---|---|---|
| D-900 | test | quelque chose casse | **CODE** | Toujours vérifier X avant Y | OUVERT |
"""


# ─────────────────────────── B-37 ───────────────────────────
def test_import_lessons_is_idempotent_and_appends(tmp_path):
    ledger = tmp_path / "L.md"; ledger.write_text(LEDGER)
    lessons = tmp_path / "lessons.jsonl"
    r1 = import_ledger.import_lessons(ledger, lessons)
    assert r1["added"] == 1 and lessons.is_file()
    r2 = import_ledger.import_lessons(ledger, lessons)     # re-run → no new lesson
    assert r2["added"] == 0 and r2["total"] == r1["total"]


def test_sync_only_reimports_when_ledger_is_newer(tmp_path):
    ledger = tmp_path / "L.md"; ledger.write_text(LEDGER)
    lessons = tmp_path / "lessons.jsonl"
    marker = tmp_path / "marker.json"
    first = ledger_sync.sync_if_stale(ledger=ledger, lessons=lessons, analysis=None, marker=marker)
    assert first["synced"] is True and first["added"] == 1
    again = ledger_sync.sync_if_stale(ledger=ledger, lessons=lessons, analysis=None, marker=marker)
    assert again["synced"] is False and again["reason"] == "up-to-date"
    # touch the ledger with a NEW defect → next launch re-imports (append-only)
    import os, time
    ledger.write_text(LEDGER + "| D-901 | test | autre | **GATE** | Fais Z | OUVERT |\n")
    os.utime(ledger, (time.time() + 10, time.time() + 10))
    third = ledger_sync.sync_if_stale(ledger=ledger, lessons=lessons, analysis=None, marker=marker)
    assert third["synced"] is True and third["added"] == 1


def test_runtime_emits_ledger_synced_event(tmp_path, monkeypatch):
    # verify the WIRING (start → _sync_ledger → event) with a stub sync module, so the real
    # committed brain is never touched and the test never depends on module-load ordering.
    import joao_orchestrator.bubble.runtime as rtmod
    from joao_orchestrator.domain.models import ProjectProfile

    class _StubSync:
        called = {}
        @staticmethod
        def sync_if_stale(**kw):
            _StubSync.called = kw
            return {"synced": True, "added": 2, "total": 48}
    monkeypatch.setattr(rtmod, "_ledger_sync_mod", lambda: _StubSync)

    ws = tmp_path / "ws"; ws.mkdir()
    for a in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.invalid"], ["git", "config", "user.name", "t"]):
        subprocess.run(a, cwd=ws, check=True)
    (ws / "m.py").write_text("V=1\n"); (ws / "t.py").write_text("from m import V\nassert V==1\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True); subprocess.run(["git", "commit", "-qm", "b"], cwd=ws, check=True)
    profile = ProjectProfile(project_id="p", display_name="p", repository_root=str(ws),
                             allowed_write_paths=["m.py"], forbidden_paths=[])
    rt = rtmod.RunRuntime(tmp_path / "state", builder=rtmod.SandboxBuilder(lambda *_: {"ok": True}),
                          profiles=rtmod.LocalProfileAdapter(), ledger_sync=True)
    run_id = rt.start(project_id="p", workspace=ws, mission="x", targeted_tests=[[sys.executable, "t.py"]],
                      full_tests=[[sys.executable, "t.py"]], profile=profile)
    events = [e for e in rt.events(run_id) if e["kind"] == "ledger_synced"]
    assert events and events[0]["synced"] is True and events[0]["added"] == 2
    # a default-constructed runtime (ledger_sync off) must NOT sync
    rt2 = rtmod.RunRuntime(tmp_path / "state2", builder=rtmod.SandboxBuilder(lambda *_: {"ok": True}),
                           profiles=rtmod.LocalProfileAdapter())
    run2 = rt2.start(project_id="p", workspace=ws, mission="x", targeted_tests=[[sys.executable, "t.py"]],
                     full_tests=[[sys.executable, "t.py"]], profile=profile)
    assert not any(e["kind"] == "ledger_synced" for e in rt2.events(run2))


# ─────────────────────────── B-24 ───────────────────────────
def _repo(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    for a in (["git", "init", "-q"], ["git", "config", "user.email", "t@t.invalid"], ["git", "config", "user.name", "t"]):
        subprocess.run(a, cwd=ws, check=True)
    (ws / "module.py").write_text("VALUE = 1\n")
    (ws / "test_module.py").write_text("from module import VALUE\nassert VALUE == 2\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True); subprocess.run(["git", "commit", "-qm", "b"], cwd=ws, check=True)
    return ws


def test_smoke_mission_selects_haiku_tier(tmp_path, allow_test_write_tier):
    ws = _repo(tmp_path)
    rd = tmp_path / "run"; rd.mkdir()
    (rd / "run.json").write_text(json.dumps({"run_id": "r", "workspace": str(ws), "critical": True,
                                             "smoke": True, "targeted_tests": [],
                                             "full_tests": [[sys.executable, "test_module.py"]]}))
    seen_models = []

    def glm_broken(_ws, _tf, out, _al, angle):
        (Path(_ws) / "module.py").write_text("VALUE = 0\n"); Path(out).write_text("{}\n")
        return {"ok": True, "returncode": 0, "output": str(out), "real_cost": 0.0, "duration_s": 0.01}

    def fake_claude_factory(exe, timeout, model):  # capture the tier the smoke mission picked
        seen_models.append(model)
        def _run(_ws, _tf, out, _al, angle):
            (Path(_ws) / "module.py").write_text("VALUE = 2\n"); Path(out).write_text("{}\n")
            return {"ok": True, "returncode": 0, "output": str(out), "real_cost": 0.0, "duration_s": 0.01}
        return _run

    import joao_orchestrator.providers.cascade_runtime as cr
    orig = cr.real_claude_runner
    cr.real_claude_runner = fake_claude_factory
    try:
        builder = CascadeBuilder(glm_runner=glm_broken)  # not injected → build() will tier by smoke
        out = builder.build("fix", ws, rd, ["module.py"], correction=False)
    finally:
        cr.real_claude_runner = orig
    assert out["ok"] and out["tier"] == Tier.CLAUDE
    assert "haiku" in seen_models  # smoke → cheap tier, never sonnet/opus
