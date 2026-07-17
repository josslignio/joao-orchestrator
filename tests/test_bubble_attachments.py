"""V1.4 BLOC E — composer attachments reach the run workspace (SHA-verified)."""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_routing import runtime  # noqa: E402

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def http(api, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        api.url + path.lstrip("/"), data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def test_attachments_reach_workspace_and_are_sha_recorded(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        note = b"contexte fourni par l'utilisateur\n"
        launched = http(api, "quick-missions", {
            "mission": "ajoute une fonction et des tests", "builder_name": "glm", "review_mode": "none",
            "attachments": [
                {"name": "notes.txt", "content_b64": base64.b64encode(note).decode()},
                {"name": "logo.png", "content_b64": base64.b64encode(PNG).decode()},
            ]})
        run_id = launched["run_id"]
        assert {a["name"] for a in launched["attachments"]} == {"notes.txt", "logo.png"}
        api.workers[run_id].join(timeout=25)
        # Evidence records both, with correct SHA-256
        run = api.runtime.get(run_id)
        by_name = {a["name"]: a for a in run["attachments"]}
        assert by_name["notes.txt"]["sha256"] == hashlib.sha256(note).hexdigest()
        assert by_name["logo.png"]["sha256"] == hashlib.sha256(PNG).hexdigest()
        # The files physically arrived in the run workspace inputs/ directory
        workspace = Path(run["workspace"])
        assert (workspace / "inputs" / "notes.txt").read_bytes() == note
        assert (workspace / "inputs" / "logo.png").read_bytes() == PNG
    finally:
        api.close()


def test_bad_attachments_fail_loud_never_silent(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        for bad in (
            [{"name": "../escape.txt", "content_b64": base64.b64encode(b"x").decode()}],
            [{"name": "ok.txt", "content_b64": "not-valid-base64!!!"}],
            [{"name": ".hidden", "content_b64": base64.b64encode(b"x").decode()}],
        ):
            request = urllib.request.Request(
                api.url + "quick-missions",
                data=json.dumps({"mission": "m", "builder_name": "glm",
                                 "review_mode": "none", "attachments": bad}).encode(),
                method="POST", headers={"Content-Type": "application/json", "X-JOAO-Token": api.token})
            try:
                urllib.request.urlopen(request, timeout=10)
                raise AssertionError(f"bad attachment accepted: {bad}")
            except urllib.error.HTTPError as error:
                assert error.code == 409  # clear error, never silent success
    finally:
        api.close()


def test_brand_assets_are_served(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        for route, magic in (("favicon.ico", None), ("assets/mascot.png", b"\x89PNG")):
            with urllib.request.urlopen(api.url + route, timeout=10) as response:
                body = response.read()
                assert len(body) > 1000
                if magic:
                    assert body.startswith(magic)
    finally:
        api.close()
