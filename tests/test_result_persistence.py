"""V1.3 — F4/F11: results are served from evidence, forever, repair loops included."""
from __future__ import annotations

import io
import json
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_routing import FixtureReviewer, runtime  # noqa: E402

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402


def http(api: LocalAPIServer, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        api.url + path.lstrip("/"), data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json", "X-JOAO-Token": api.token},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def http_raw(api: LocalAPIServer, path: str):
    request = urllib.request.Request(api.url + path.lstrip("/"),
                                     headers={"X-JOAO-Token": api.token})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read(), dict(response.headers)


class OneRepairLoopReviewer(FixtureReviewer):
    """P1 on the first build review, ACCEPT everywhere afterwards (forces one repair)."""

    def __init__(self):
        super().__init__("claude-fixture")
        self.flagged = False

    def review_stage(self, run, run_dir, stage):
        if stage == "build" and not self.flagged:
            self.flagged = True
            result = super().review_stage(run, run_dir, stage)
            result.update({"ok": False, "decision": "p1",
                           "finding": "P1 fixture: force one bounded repair loop"})
            return result
        return super().review_stage(run, run_dir, stage)


def launch(api, review_mode="none"):
    launched = http(api, "quick-missions", {"mission": "mission persistance résultat",
                                            "builder_name": "glm", "review_mode": review_mode})
    api.workers[launched["run_id"]].join(timeout=30)
    return launched["run_id"]


def assert_result_fully_served(api, run_id):
    summary = http(api, f"runs/{run_id}/result")
    assert summary["result_available"] is True
    delivered = [f for f in summary["files"] if f["exists"]]
    assert delivered, "no deliverable served"
    assert all(f.get("source") == "evidence" for f in summary["files"])
    text_path = next(f["path"] for f in delivered if f["path"].endswith("todo.py"))
    preview = http(api, f"runs/{run_id}/result/file?path=" + urllib.request.quote(text_path))
    assert preview["content"] == "VALUE = 2\n"
    raw, _ = http_raw(api, f"runs/{run_id}/result/zip")
    names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())
    assert "deliverables/todo.py" in names and "final-diff.patch" in names


def test_downloads_survive_sandbox_cleanup(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        run_id = launch(api)
        assert api.runtime.get(run_id)["status"] == "needs_approval"
        shutil.rmtree(api.runtime.get(run_id)["workspace"])  # the sandbox dies
        assert_result_fully_served(api, run_id)
    finally:
        api.close()


def test_downloads_survive_ui_restart_after_cleanup(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        run_id = launch(api)
        workspace = api.runtime.get(run_id)["workspace"]
    finally:
        api.close()
    shutil.rmtree(workspace)
    fresh = LocalAPIServer(runtime(tmp_path))
    fresh.serve_in_thread()
    try:
        assert_result_fully_served(fresh, run_id)
    finally:
        fresh.close()


def test_result_panel_data_present_after_a_forced_repair_loop(tmp_path):
    """F11 regression: a run that went through a repair loop still serves its result."""
    api = LocalAPIServer(runtime(tmp_path, claude=OneRepairLoopReviewer()))
    api.serve_in_thread()
    try:
        run_id = launch(api, review_mode="claude")
        run = api.runtime.get(run_id)
        assert run["status"] == "needs_approval"
        assert run["corrections_used"] == 1, "the repair loop did not happen"
        assert run["result_available"] is True
        shutil.rmtree(run["workspace"])
        assert_result_fully_served(api, run_id)
    finally:
        api.close()
