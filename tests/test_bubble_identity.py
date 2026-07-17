"""V1.2 — the Bubble presents itself as JOÃO.AI everywhere."""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_provider_routing import runtime  # noqa: E402

from joao_orchestrator.bubble.api import LocalAPIServer  # noqa: E402


def test_ui_page_is_branded_joao_ai(tmp_path):
    api = LocalAPIServer(runtime(tmp_path))
    api.serve_in_thread()
    try:
        with urllib.request.urlopen(urllib.request.Request(api.url), timeout=10) as response:
            page = response.read().decode()
        assert "<title>JOÃO.AI</title>" in page
        # A1 wordmark + bloom doux (locked brand decision)
        assert 'class="a1">JOÃO<small>.AI</small>' in page
        assert 'class="bloomD">JOÃO<small>.AI</small>' in page
        # neon mascot served in the header
        assert "/assets/mascot.png" in page
        assert "<title>JOAO</title>" not in page
    finally:
        api.close()
