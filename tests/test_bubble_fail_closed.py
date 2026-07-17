"""V1.4 BLOC A — fail-closed fixes proven by the V13 findings."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_routing import FixtureBuilder, FixtureReviewer, runtime  # noqa: E402

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402
from joao_orchestrator.bubble.runtime import (  # noqa: E402
    EMPTY_DIFF_SHA256, mission_allowed_paths, parse_test_cases,
)


def http(api, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        api.url + path.lstrip("/"), data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


# ---------- A2/F9: test CASES, not commands ----------
def test_parse_test_cases_counts_real_cases():
    assert parse_test_cases([{"ok": True, "stderr": "Ran 5 tests in 0.01s\nOK\n"}]) == \
        {"cases_collected": 5, "cases_passed": 5}
    # unittest on an empty workspace: 0 collected, never green
    assert parse_test_cases([{"ok": True, "stderr": "Ran 0 tests in 0.000s\nOK\n"}]) == \
        {"cases_collected": 0, "cases_passed": 0}
    # a failing unittest run reports failures
    got = parse_test_cases([{"ok": False, "stderr": "Ran 3 tests in 0.01s\n\nFAILED (failures=1)\n"}])
    assert got == {"cases_collected": 3, "cases_passed": 2}
    # pytest style
    assert parse_test_cases([{"ok": False, "stdout": "2 passed, 1 failed in 0.1s"}]) == \
        {"cases_collected": 3, "cases_passed": 2}


# ---------- A5b/F14: allowed paths from the mission ----------
def test_mission_allowed_paths_are_derived_from_the_mission():
    paths = mission_allowed_paths("Crée roman.py qui convertit les nombres romains + tests")
    assert "roman.py" in paths
    assert "src/" in paths and "tests/" in paths
    # traversal and absolute paths are never admitted
    evil = mission_allowed_paths("write ../../etc/passwd and /root/x.py")
    assert not any(p.startswith("/") or ".." in p for p in evil)


def test_needs_approval_without_deliverable_never_says_result_ready(tmp_path):
    """A plan-gate reviewer disagreement resolves to needs_approval with no build
    (e.g. Codex QUOTA_BLOCKED vs Claude ACCEPT). Narration must not lie."""
    rt = runtime(tmp_path)
    honest = rt.narrate({"run_id": "x", "status": "needs_approval", "result_available": False,
                         "builder_name": "glm", "reviewer_names": ["claude", "codex"]})
    assert "Résultat prêt" not in honest
    assert "désaccord" in honest and "aucun livrable" in honest
    # with a real deliverable it still announces the result
    ready = rt.narrate({"run_id": "x", "status": "needs_approval", "result_available": True,
                        "builder_name": "glm", "reviewer_names": [], "review_policy": "none"})
    assert "Résultat prêt" in ready


class NothingBuilder(FixtureBuilder):
    """A builder that runs but creates no file (the V13-F3/T5 empty-diff bug)."""
    def build(self, mission, workspace, run_dir, allowed, correction):
        result = super().build(mission, workspace, run_dir, allowed, correction)
        # undo the file the fixture wrote — deliver nothing
        (Path(workspace) / "todo.py").unlink(missing_ok=True)
        return result


def test_empty_diff_is_failed_nothing_produced_never_green(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, builder=NothingBuilder()))
    api.serve_in_thread()
    try:
        launched = http(api, "quick-missions", {"mission": "écris un poème inspiré de Verlaine",
                                                "builder_name": "glm", "review_mode": "none"})
        api.workers[launched["run_id"]].join(timeout=25)
        run = api.runtime.get(launched["run_id"])
        assert run["status"] == "failed"
        assert run["nothing_produced"] is True
        assert run["final_diff_sha256"] == EMPTY_DIFF_SHA256
        assert "Rien n'a été produit" in run["narration"]
        # never a green result, never needs_approval
        assert run["status"] != "needs_approval"
        summary = http(api, f"runs/{launched['run_id']}/result")
        assert not [f for f in summary["files"] if f["exists"]]
    finally:
        api.close()


class VerdictReviewer(FixtureReviewer):
    """A reviewer that BLOCKS the plan with a real, intelligent finding."""
    VERDICT = "The plan encodes a mathematical contradiction: 9 is not prime (9 = 3×3)."

    def __init__(self):
        super().__init__("claude-fixture", decision="block")

    def review_stage(self, run, run_dir, stage):
        result = super().review_stage(run, run_dir, stage)
        result["finding"] = self.VERDICT
        result["verdict"] = "BLOCK"
        return result


def test_blocked_card_shows_the_real_reviewer_verdict(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, claude=VerdictReviewer()))
    api.serve_in_thread()
    try:
        launched = http(api, "quick-missions", {"mission": "implémente is_prime avec un test disant que 9 est premier",
                                                "builder_name": "glm", "review_mode": "claude"})
        api.workers[launched["run_id"]].join(timeout=25)
        run = api.runtime.get(launched["run_id"])
        assert run["status"] == "blocked"
        # A6: the intelligent verdict is visible, not a generic 'plan review blocked'
        assert "9 is not prime" in run["block_cause"]
        assert run["block_cause"] != "plan review blocked"
        assert run["block_verdicts"] and run["block_verdicts"][-1]["finding"] == VerdictReviewer.VERDICT
    finally:
        api.close()


def test_quick_launch_allows_a_mission_named_file(tmp_path):
    """F14 non-regression: a roman.py mission passes the plan gate (path in scope)."""
    class RomanBuilder(FixtureBuilder):
        def build(self, mission, workspace, run_dir, allowed, correction):
            result = super().build(mission, workspace, run_dir, allowed, correction)
            (Path(workspace) / "roman.py").write_text("def to_roman(n):\n    return 'I' * n\n")
            (Path(workspace) / "todo.py").unlink(missing_ok=True)
            return result

    api = LocalAPIServer(runtime(tmp_path, builder=RomanBuilder()))
    api.serve_in_thread()
    try:
        launched = http(api, "quick-missions", {"mission": "Crée roman.py, une fonction to_roman, avec des tests",
                                                "builder_name": "glm", "review_mode": "none"})
        api.workers[launched["run_id"]].join(timeout=25)
        run = api.runtime.get(launched["run_id"])
        # roman.py was created and NOT flagged as an out-of-scope violation
        assert run["status"] == "needs_approval", run.get("block_cause")
        summary = http(api, f"runs/{launched['run_id']}/result")
        assert any(f["path"] == "roman.py" and f["exists"] for f in summary["files"])
    finally:
        api.close()
