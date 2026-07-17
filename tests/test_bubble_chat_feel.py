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
    # F1: Enter submits, Shift+Enter is a newline.
    assert "e.key==='Enter'&&!e.shiftKey" in page and "e.preventDefault()" in page
    # F2: one Stop button on active cards; no pause button anywhere. Resume
    # survives ONLY on legacy paused cards (nothing in the UI creates new ones).
    assert '"pause"' not in page and 'data-act="pause"' not in page
    assert re.search(r'building:\["stop"\]', page)
    assert 'needs_approval:["approve","reject"]' in page
    assert 'paused:["resume","stop"]' in page
    # F5: the user's mission on the card.
    assert "Tu as demandé" in page and "mission_excerpt" in page
    # F3: narration + step elapsed + ETA.
    assert "narration" in page and "step_elapsed_seconds" in page
    assert "résultat estimé dans" in page and "eta_seconds" in page
    # F7/F11: needs_approval auto-opens the result, errors are honest.
    assert "s.status==='needs_approval'&&!c.userClosed" in page
    assert "Résultat indisponible" in page
    # F9: the timeline panel.
    assert "Ce qui s\\'est passé" in page or "Ce qui s'est passé" in page
    assert "/timeline" in page
    # F10: decision consequences.
    assert "résultat conservé, run archivé accepté" in page
    assert "résultat écarté, run archivé rejeté" in page


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
    # Night gradient + powder grain.
    assert "linear-gradient(160deg,#0b0e1a,#1a1440)" in page
    assert "feTurbulence" in page
    # Wordmark: animated gradient + chromatic ghost offsets.
    assert "hueShift" in page and "background-clip:text" in page
    assert "text-shadow:2px 0" in page
    # Mascot placeholder + mockup card vocabulary.
    assert 'class="mascot"' in page
    assert "runcard" in page and "run-head" in page and "gates" in page
    assert 'class="st ' in page and "lbl-rev" in page
    # Composer stays visible: the mission form is a fixed card above the scroll list.
    assert "Nouvelle mission" in page and "inputbox" in page
