"""V1.3 — the chat-feel behaviors and the validated look, as served page markers."""
from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_routing import runtime  # noqa: E402

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402


def served_page(tmp_path) -> str:
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        with urllib.request.urlopen(urllib.request.Request(api.url), timeout=10) as response:
            return response.read().decode()
    finally:
        api.close()


def test_chat_feel_behaviors_are_wired(tmp_path):
    page = served_page(tmp_path)
    # V13-F1: Enter submits, Shift+Enter newline; composer anchored at the bottom.
    assert "e.key==='Enter'&&!e.shiftKey" in page and "e.preventDefault()" in page
    assert "composer-zone" in page
    # V13-F11: approve/reject removed from the normal flow; result shows directly.
    assert 'data-act="approve"' not in page and 'data-act="reject"' not in page
    assert "resultBlock" in page
    # a single Stop control on active runs (no pause/resume UI)
    assert "data-stop" in page and 'data-act="pause"' not in page
    # V13-F5: the user's mission in its own bubble.
    assert "msg-user" in page and "mission_display" in page
    # V13-F3: narration + step elapsed + ETA.
    assert "narration" in page and "step_elapsed_seconds" in page
    assert "résultat estimé dans" in page and "eta_seconds" in page
    # V13-F10: attachments (📎, drag-drop, paste).
    assert "file-input" in page and "data-rmatt" in page and "addFiles" in page
    # V13-F12: artifacts side panel.
    assert "openArtifact" in page and 'id="artifacts"' in page and "art-body" in page
    # A1/A6: honest empty result + real reviewer verdict surfaced.
    assert "nothing_produced" in page and "block_verdicts" in page
    # Self-review fixes: run attaches to the conversation captured BEFORE the
    # await; un-threaded persisted runs are adopted into a default history.
    assert "const conv=activeConvObj();\n try{const v=await req" in page
    assert "c-history" in page and "orphans" in page


def test_token_page_refuses_foreign_host_headers(tmp_path):
    import urllib.error
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        request = urllib.request.Request(api.url, headers={"Host": "evil.example"})
        try:
            urllib.request.urlopen(request, timeout=10)
            raise AssertionError("foreign Host was served the token page")
        except urllib.error.HTTPError as error:
            assert error.code == 403
        # The legitimate loopback Host still gets the page.
        with urllib.request.urlopen(urllib.request.Request(api.url), timeout=10) as response:
            assert "TOKEN" in response.read().decode()
    finally:
        api.close()


def test_validated_look_is_applied(tmp_path):
    page = served_page(tmp_path)
    # Night P1 gradient + powder grain.
    assert "linear-gradient(160deg,#0b0e1a,#1a1440)" in page
    assert "feTurbulence" in page
    # A1 wordmark + bloom doux (the locked brand; the chrome/ghost F2 was abandoned).
    assert "@keyframes flowS" in page and "background-clip:text" in page
    assert 'class="a1"' in page and 'class="bloomD"' in page
    # neon mascot 96px in the header.
    assert 'class="mascot"' in page and "height:96px" in page
    # Claude-clone layout vocabulary: sidebar conversations + run cards + bottom composer.
    assert "sidebar" in page and 'id="convs"' in page
    assert "runcard" in page and "run-head" in page and "gates" in page
    assert 'class="st ' in page and "lbl-rev" in page
    assert "Nouvelle conversation" in page and "inputbox" in page
