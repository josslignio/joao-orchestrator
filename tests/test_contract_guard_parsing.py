"""Tests for JOÃO Contract Guard Parsing - Fail-Closed Behavior"""
import json
import os
import tempfile
from joao_orchestrator.governance.contract_guard import PolicyVerdict, compare_contracts


def test_none_frozen_path_returns_deny():
    """None frozen_path must return DENY, never raise"""
    with tempfile.TemporaryDirectory() as tmpdir:
        candidate = os.path.join(tmpdir, "c.json")
        with open(candidate, 'w') as f:
            json.dump({"governor_steps": {}, "stage_a": {}}, f)
        verdict, violations = compare_contracts(None, candidate, "h")
        assert verdict == PolicyVerdict.DENY
        assert "Invalid path type" in violations


def test_none_candidate_path_returns_deny():
    """None candidate_path must return DENY, never raise"""
    with tempfile.TemporaryDirectory() as tmpdir:
        frozen = os.path.join(tmpdir, "f.json")
        with open(frozen, 'w') as f:
            json.dump({"governor_steps": {}, "stage_a": {}}, f)
        verdict, violations = compare_contracts(frozen, None, "h")
        assert verdict == PolicyVerdict.DENY


def test_missing_frozen_returns_deny():
    """Missing frozen file returns DENY"""
    with tempfile.TemporaryDirectory() as tmpdir:
        candidate = os.path.join(tmpdir, "c.json")
        with open(candidate, 'w') as f:
            json.dump({"governor_steps": {}, "stage_a": {}}, f)
        verdict, violations = compare_contracts("nonexistent.json", candidate, "h")
        assert verdict == PolicyVerdict.DENY


def test_invalid_json_frozen_returns_deny():
    """Invalid JSON in frozen returns DENY"""
    with tempfile.TemporaryDirectory() as tmpdir:
        frozen = os.path.join(tmpdir, "f.json")
        candidate = os.path.join(tmpdir, "c.json")
        with open(frozen, 'w') as f:
            f.write("{invalid")
        with open(candidate, 'w') as f:
            json.dump({"governor_steps": {}, "stage_a": {}}, f)
        verdict, violations = compare_contracts(frozen, candidate, "h")
        assert verdict == PolicyVerdict.DENY


def test_non_object_root_returns_deny():
    """Array root must return DENY"""
    with tempfile.TemporaryDirectory() as tmpdir:
        frozen = os.path.join(tmpdir, "f.json")
        candidate = os.path.join(tmpdir, "c.json")
        with open(frozen, 'w') as f:
            json.dump([1, 2, 3], f)
        with open(candidate, 'w') as f:
            json.dump({"governor_steps": {}, "stage_a": {}}, f)
        verdict, violations = compare_contracts(frozen, candidate, "h")
        assert verdict == PolicyVerdict.DENY


def test_missing_governor_steps_returns_deny():
    """Missing governor_steps field returns DENY"""
    with tempfile.TemporaryDirectory() as tmpdir:
        frozen = os.path.join(tmpdir, "f.json")
        candidate = os.path.join(tmpdir, "c.json")
        with open(frozen, 'w') as f:
            json.dump({"stage_a": {}}, f)
        with open(candidate, 'w') as f:
            json.dump({"governor_steps": {}, "stage_a": {}}, f)
        verdict, violations = compare_contracts(frozen, candidate, "h")
        assert verdict == PolicyVerdict.DENY
        assert "invalid structure" in violations[0].lower()


def test_governor_steps_not_dict_returns_deny():
    """governor_steps as array returns DENY"""
    with tempfile.TemporaryDirectory() as tmpdir:
        frozen = os.path.join(tmpdir, "f.json")
        candidate = os.path.join(tmpdir, "c.json")
        with open(frozen, 'w') as f:
            json.dump({"governor_steps": [], "stage_a": {}}, f)
        with open(candidate, 'w') as f:
            json.dump({"governor_steps": {}, "stage_a": {}}, f)
        verdict, violations = compare_contracts(frozen, candidate, "h")
        assert verdict == PolicyVerdict.DENY


def test_missing_stage_a_returns_deny():
    """Missing stage_a returns DENY"""
    with tempfile.TemporaryDirectory() as tmpdir:
        frozen = os.path.join(tmpdir, "f.json")
        candidate = os.path.join(tmpdir, "c.json")
        with open(frozen, 'w') as f:
            json.dump({"governor_steps": {}}, f)
        with open(candidate, 'w') as f:
            json.dump({"governor_steps": {}, "stage_a": {}}, f)
        verdict, violations = compare_contracts(frozen, candidate, "h")
        assert verdict == PolicyVerdict.DENY


def test_valid_structure_returns_allow():
    """Valid structure returns ALLOW"""
    with tempfile.TemporaryDirectory() as tmpdir:
        frozen = os.path.join(tmpdir, "f.json")
        candidate = os.path.join(tmpdir, "c.json")
        data = {"governor_steps": {}, "stage_a": {}}
        with open(frozen, 'w') as f:
            json.dump(data, f)
        with open(candidate, 'w') as f:
            json.dump(data, f)
        verdict, violations = compare_contracts(frozen, candidate, "h")
        assert verdict == PolicyVerdict.ALLOW
        assert len(violations) == 0


def test_compare_never_raises_for_user_input():
    """compare_contracts must never raise for any user-controlled input"""
    with tempfile.TemporaryDirectory() as tmpdir:
        candidate = os.path.join(tmpdir, "c.json")
        with open(candidate, 'w') as f:
            json.dump({"governor_steps": {}, "stage_a": {}}, f)
        # Various malicious inputs - none should raise
        assert compare_contracts(None, candidate, "h")[0] == PolicyVerdict.DENY
        assert compare_contracts(candidate, None, "h")[0] == PolicyVerdict.DENY
        assert compare_contracts("", candidate, "h")[0] == PolicyVerdict.DENY
        assert compare_contracts(123, candidate, "h")[0] == PolicyVerdict.DENY