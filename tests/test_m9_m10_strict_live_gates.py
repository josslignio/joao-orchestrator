from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_m9_requires_one_exact_json_object():
    module = load_script("run_m9_external_review.py")
    assert module.parse_exact_decision('{"verdict":"ACCEPT","reason":"clean"}') == {
        "verdict": "ACCEPT",
        "reason": "clean",
    }
    with pytest.raises(json.JSONDecodeError):
        module.parse_exact_decision('Result: {"verdict":"ACCEPT","reason":"clean"}')
    with pytest.raises(ValueError, match="invalid verdict"):
        module.parse_exact_decision('{"verdict":"INVALID","reason":"no"}')


def test_m10_accepts_only_exact_marker():
    module = load_script("run_m10_supervisor_live.py")
    assert module.is_exact_marker("JOAO_M10_OK") is True
    # The marker must be byte-exact: no whitespace, no newlines, no extra chars.
    # This was a real Codex BLOCK finding — stripping allowed spoofed markers.
    assert module.is_exact_marker("  JOAO_M10_OK\n") is False
    assert module.is_exact_marker("JOAO_M10_OK extra") is False
    assert module.is_exact_marker("You've hit your weekly limit") is False
    assert module.is_exact_marker("") is False
