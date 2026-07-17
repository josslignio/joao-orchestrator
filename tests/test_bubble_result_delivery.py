"""V1.2 — a human can consult and download what was built before deciding."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_routing import runtime  # noqa: E402

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


def approved_run(api: LocalAPIServer) -> str:
    launched = http(api, "quick-missions", {"mission": "construis la fixture TODO",
                                            "builder_name": "glm", "review_mode": "none"})
    api.workers[launched["run_id"]].join(timeout=20)
    assert api.runtime.get(launched["run_id"])["status"] == "needs_approval"
    return launched["run_id"]


def test_result_summary_names_files_and_tests(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        run_id = approved_run(api)
        assert http(api, f"runs/{run_id}")["result_available"] is True
        summary = http(api, f"runs/{run_id}/result")
        assert summary["result_available"] is True
        paths = [item["path"] for item in summary["files"] if item["exists"]]
        assert "todo.py" in paths
        entry = next(item for item in summary["files"] if item["path"] == "todo.py")
        assert entry["is_text"] is True and entry["bytes"] > 0 and entry["sha256"]
        assert summary["tests"]["all_passed"] is True
        assert summary["tests"]["passed"] == summary["tests"]["commands"] > 0
        assert summary["final_diff_sha256"]
    finally:
        api.close()


def test_result_file_preview_and_download(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        run_id = approved_run(api)
        preview = http(api, f"runs/{run_id}/result/file?path=todo.py")
        assert preview["content"] == "VALUE = 2\n"
        assert preview["is_text"] is True and preview["truncated"] is False
        raw, headers = http_raw(api, f"runs/{run_id}/result/file?path=todo.py&download=1")
        assert raw == b"VALUE = 2\n"
        assert 'filename="todo.py"' in headers["Content-Disposition"]
    finally:
        api.close()


def test_result_zip_bundles_deliverables_diff_and_summary(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        run_id = approved_run(api)
        raw, headers = http_raw(api, f"runs/{run_id}/result/zip")
        assert headers["Content-Type"] == "application/zip"
        assert f'{run_id}-result.zip' in headers["Content-Disposition"]
        bundle = zipfile.ZipFile(io.BytesIO(raw))
        names = set(bundle.namelist())
        assert "deliverables/todo.py" in names
        assert "final-diff.patch" in names
        assert "result-summary.json" in names
        assert bundle.read("deliverables/todo.py") == b"VALUE = 2\n"
        summary = json.loads(bundle.read("result-summary.json"))
        assert summary["run_id"] == run_id
    finally:
        api.close()


def test_result_file_refuses_workspace_escape(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        run_id = approved_run(api)
        for evil in ("../../secrets.txt", "/etc/passwd", "..", "."):
            request = urllib.request.Request(
                api.url + f"runs/{run_id}/result/file?path={urllib.request.quote(evil, safe='')}",
                headers={"X-JOAO-Token": api.token})
            try:
                urllib.request.urlopen(request, timeout=10)
                raise AssertionError(f"escape not refused: {evil}")
            except urllib.error.HTTPError as error:
                assert error.code == 404, evil
    finally:
        api.close()


def test_ui_page_ships_the_result_panel(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        with urllib.request.urlopen(urllib.request.Request(api.url), timeout=10) as response:
            page = response.read().decode()
        assert "resultBlock" in page and "result_available" in page
        assert 'data-res="diff"' in page and 'data-res="zip"' in page
        assert 'data-res="preview"' in page and 'data-res="download"' in page
        assert "Télécharger tout (zip)" in page
        assert ".resultbox" in page
    finally:
        api.close()
