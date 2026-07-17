"""Regression tests for the human-gate findings (HUMAN_SMOKE_TEST_FINDINGS_20260717)."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_routing import FixtureBuilder, FixtureReviewer, runtime  # noqa: E402

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402


QUOTA_MESSAGE = ("You've hit your usage limit. Upgrade to Pro or "
                 "try again at Jul 23rd, 2026 6:22 AM.")


class QuotaBlockedReviewer(FixtureReviewer):
    """Simulates a Codex reviewer dying on an exhausted subscription."""

    def __init__(self):
        super().__init__("codex-fixture", decision="block")

    def review_stage(self, run, run_dir, stage):
        result = super().review_stage(run, run_dir, stage)
        result["reason"] = QUOTA_MESSAGE
        result["stdout_tail"] = QUOTA_MESSAGE
        return result


def http(api: LocalAPIServer, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        api.url + path.lstrip("/"), data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json", "X-JOAO-Token": api.token},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def blocked_quota_run(api: LocalAPIServer) -> str:
    launched = http(api, "quick-missions", {"mission": "mission bloquée quota",
                                            "builder_name": "glm", "review_mode": "codex"})
    api.workers[launched["run_id"]].join(timeout=20)
    assert api.runtime.get(launched["run_id"])["status"] == "blocked"
    return launched["run_id"]


def test_every_control_click_gets_explicit_feedback_on_a_blocked_run(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, codex=QuotaBlockedReviewer()))
    api.serve_in_thread()
    try:
        run_id = blocked_quota_run(api)
        for action in ("pause", "resume", "stop", "approve"):
            result = http(api, f"runs/{run_id}/{action}", {})
            assert result["accepted"] is False, action
            assert result["action"] == action
            assert "non applicable" in result["reason"]
            assert result["status"] == "blocked"
        rejected = http(api, f"runs/{run_id}/reject", {})
        assert rejected["accepted"] is True
        assert rejected["status"] == "stopped"
        assert "→" in rejected["reason"]
    finally:
        api.close()


def test_reject_frees_the_ui_and_a_new_run_launches(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, codex=QuotaBlockedReviewer()))
    api.serve_in_thread()
    try:
        first = blocked_quota_run(api)
        assert http(api, f"runs/{first}/reject", {})["status"] == "stopped"
        relaunch = http(api, "quick-missions", {"mission": "run suivant après reject",
                                                "builder_name": "glm", "review_mode": "none"})
        api.workers[relaunch["run_id"]].join(timeout=20)
        assert api.runtime.get(relaunch["run_id"])["status"] == "needs_approval"
    finally:
        api.close()


def test_blocked_run_explains_cause_and_unblock_condition(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, codex=QuotaBlockedReviewer()))
    api.serve_in_thread()
    try:
        run_id = blocked_quota_run(api)
        run = http(api, f"runs/{run_id}")
        assert run["quota_blocked"] is True
        assert "quota" in run["block_cause"].lower()
        assert "Jul 23rd" in run["block_cause"]
        assert "Reject" in run["unblock_hint"] and "Retry" in run["unblock_hint"]
    finally:
        api.close()


def test_persisted_runs_are_listed_after_a_server_restart(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, codex=QuotaBlockedReviewer()))
    api.serve_in_thread()
    try:
        run_id = blocked_quota_run(api)
    finally:
        api.close()
    fresh = LocalAPIServer(runtime(tmp_path, codex=QuotaBlockedReviewer()))
    fresh.serve_in_thread()
    try:
        listed = http(fresh, "runs")["runs"]
        assert [item["run_id"] for item in listed] == [run_id]
        assert listed[0]["status"] == "blocked"
        assert listed[0]["builder_name"] == "glm"
    finally:
        fresh.close()


def test_capabilities_expose_the_last_codex_quota_block(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, codex=QuotaBlockedReviewer()))
    api.serve_in_thread()
    try:
        run_id = blocked_quota_run(api)
        capabilities = http(api, "capabilities")
        warning = capabilities["codex"]["quota_warning"]
        assert warning is not None
        assert warning["at"]
        assert "quota" in warning["message"].lower()
        # A stopped run that hit the quota is still live evidence Codex is dead.
        http(api, f"runs/{run_id}/reject", {})
        warning = http(api, "capabilities")["codex"]["quota_warning"]
        assert warning is not None and "quota" in warning["message"].lower()
    finally:
        api.close()


def test_retry_with_exhausted_budget_is_refused_with_guidance(tmp_path):
    api = LocalAPIServer(runtime(tmp_path, codex=QuotaBlockedReviewer()))
    api.serve_in_thread()
    try:
        run_id = blocked_quota_run(api)
        run = api.runtime._read(run_id)
        run["corrections_used"] = run["max_corrections"]
        api.runtime._write(run)
        result = http(api, f"runs/{run_id}/retry", {})
        assert result["accepted"] is False
        assert "budget de réparation épuisé" in result["reason"]
        assert "Reject" in result["reason"]
    finally:
        api.close()


def test_quota_warning_never_fires_from_non_codex_evidence(tmp_path):
    class QuotaWordingClaudeReviewer(FixtureReviewer):
        def __init__(self):
            super().__init__("claude-fixture", decision="block")

        def review_stage(self, run, run_dir, stage):
            result = super().review_stage(run, run_dir, stage)
            result["reason"] = QUOTA_MESSAGE  # quota-sounding text from a NON-codex reviewer
            return result

    api = LocalAPIServer(runtime(tmp_path, claude=QuotaWordingClaudeReviewer()))
    api.serve_in_thread()
    try:
        launched = http(api, "quick-missions", {"mission": "mission au sujet des rate limits",
                                                "builder_name": "glm", "review_mode": "claude"})
        api.workers[launched["run_id"]].join(timeout=20)
        run = api.runtime.get(launched["run_id"])
        assert run["status"] == "blocked"
        assert http(api, "capabilities")["codex"]["quota_warning"] is None
        # ... and the block explanation must not claim a Codex quota either.
        assert run["quota_blocked"] is False
        assert "Quota" not in (run["block_cause"] or "")
    finally:
        api.close()


def test_ui_page_ships_contextual_controls_and_collapsed_json(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        request = urllib.request.Request(api.url)
        with urllib.request.urlopen(request, timeout=10) as response:
            page = response.read().decode()
        # restored runs list + contextual action map + explicit feedback wiring
        assert "req('/runs')" in page
        assert "const ACTIONS=" in page and 'blocked:["retry","reject"]' in page
        assert 'correcting:["stop"]' in page  # the correction loop stays controllable
        assert "&quot;" in page  # esc() hardens attribute interpolation
        assert "refusée" in page and "acceptée" in page
        # collapsed evidence JSON and block-cause box
        assert "<details><summary>" in page and "block_cause" in page
        # color semantics: green ready, orange warning, red error
        assert ".ok{color:var(--green)}" in page and ".warn{color:var(--orange)}" in page and ".err{color:var(--red)}" in page
        # quota-doomed configuration warning near the selectors
        assert "quota-warning" in page and "quota_warning" in page
    finally:
        api.close()
