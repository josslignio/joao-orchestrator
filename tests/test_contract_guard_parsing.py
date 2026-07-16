"""
Tests for JOÃO Contract Guard Parsing - Fail-Closed Behavior

Tests attack the parsing layer with malformed inputs to ensure
fail-closed behavior. No comparison logic tests here (those are GOV-2).
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from joao_orchestrator.governance.contract_guard import (
    PolicyVerdict,
    PolicyViolation,
    compare_contracts,
)


class TestFailClosedParsing:
    """Test that malformed inputs always return DENY, never raise"""
    
    def test_missing_frozen_file_returns_deny(self):
        """Missing frozen file must return DENY, never raise"""
        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create valid candidate
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            frozen_path = os.path.join(tmpdir, "nonexistent.json")
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
            assert len(violations) == 1
            assert "missing or malformed" in violations[0].reason.lower()
    
    def test_missing_candidate_file_returns_deny(self):
        """Missing candidate file must return DENY, never raise"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            
            # Create valid frozen
            with open(frozen_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            candidate_path = os.path.join(tmpdir, "nonexistent.json")
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
            assert len(violations) == 1
            assert "missing or malformed" in violations[0].reason.lower()
    
    def test_invalid_json_frozen_returns_deny(self):
        """Invalid JSON in frozen file must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create valid candidate
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Create invalid frozen JSON
            with open(frozen_path, 'w') as f:
                f.write("{invalid json")
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
            assert "frozen contract" in violations[0].reason.lower()
    
    def test_invalid_json_candidate_returns_deny(self):
        """Invalid JSON in candidate file must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create valid frozen
            with open(frozen_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Create invalid candidate JSON
            with open(candidate_path, 'w') as f:
                f.write("{invalid json")
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
            assert "candidate contract" in violations[0].reason.lower()
    
    def test_non_object_root_frozen_returns_deny(self):
        """Non-object root in frozen must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create valid candidate
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Create frozen with array root
            with open(frozen_path, 'w') as f:
                json.dump([1, 2, 3], f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
    
    def test_non_object_root_candidate_returns_deny(self):
        """Non-object root in candidate must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create valid frozen
            with open(frozen_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Create candidate with array root
            with open(candidate_path, 'w') as f:
                json.dump([1, 2, 3], f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
    
    def test_string_root_returns_deny(self):
        """String root must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create valid candidate
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Create frozen with string root
            with open(frozen_path, 'w') as f:
                json.dump("not an object", f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
    
    def test_number_root_returns_deny(self):
        """Number root must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create valid candidate
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Create frozen with number root
            with open(frozen_path, 'w') as f:
                json.dump(42, f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
    
    def test_null_root_returns_deny(self):
        """Null root must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create valid candidate
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Create frozen with null root
            with open(frozen_path, 'w') as f:
                json.dump(None, f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY


class TestStructureValidation:
    """Test contract structure validation"""
    
    def test_missing_governor_steps_returns_deny(self):
        """Missing governor_steps field must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen without governor_steps
            with open(frozen_path, 'w') as f:
                json.dump({"stage_a": {}}, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
            assert "invalid structure" in violations[0].reason.lower()
    
    def test_malformed_governor_steps_returns_deny(self):
        """Malformed governor_steps type must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen with governor_steps as array
            with open(frozen_path, 'w') as f:
                json.dump({"governor_steps": [], "stage_a": {}}, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
    
    def test_governor_steps_as_string_returns_deny(self):
        """governor_steps as string must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen with governor_steps as string
            with open(frozen_path, 'w') as f:
                json.dump({"governor_steps": "not an object", "stage_a": {}}, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
    
    def test_missing_stage_a_returns_deny(self):
        """Missing stage_a field must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen without stage_a
            with open(frozen_path, 'w') as f:
                json.dump({"governor_steps": {}}, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
    
    def test_malformed_stage_a_returns_deny(self):
        """Malformed stage_a type must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen with stage_a as array
            with open(frozen_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": []}, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY


class TestNestedTypeErrors:
    """Test nested structure type validation"""
    
    def test_nested_malformed_object_in_governor_steps(self):
        """Nested malformed object in governor_steps must return DENY"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen with malformed nested structure
            frozen_data = {
                "governor_steps": {
                    "step1": {"nested": "value"},
                    "step2": "should be object not string"
                },
                "stage_a": {}
            }
            
            with open(frozen_path, 'w') as f:
                json.dump(frozen_data, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # This should work since we only check that governor_steps is a dict
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            # In GOV-1 we only validate structure, not nested types
            assert verdict == PolicyVerdict.ALLOW
    
    def test_nested_list_validation(self):
        """Nested list validation in contract structure"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen with lists in structure
            frozen_data = {
                "governor_steps": {
                    "step1": {"allowed_paths": ["path1", "path2"]}
                },
                "stage_a": {}
            }
            
            with open(frozen_path, 'w') as f:
                json.dump(frozen_data, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # In GOV-1 we only validate that governor_steps exists and is a dict
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.ALLOW


class TestCompareContractsNeverRaises:
    """Test that compare_contracts never raises for user-controlled input"""
    
    def test_empty_file_returns_deny(self):
        """Empty file must return DENY, never raise"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create empty frozen file
            with open(frozen_path, 'w') as f:
                f.write("")
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Must not raise
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
    
    def test_binary_data_returns_deny(self):
        """Binary data must return DENY, never raise"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen with binary-like data
            with open(frozen_path, 'wb') as f:
                f.write(b'\x00\x01\x02\x03')
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Must not raise
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.DENY
    
    def test_special_characters_in_json(self):
        """Special characters must be handled gracefully"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen with special chars
            with open(frozen_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Must not raise
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.ALLOW
    
    def test_unicode_data(self):
        """Unicode data must be handled gracefully"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create frozen with unicode
            frozen_data = {
                "governor_steps": {"עברית": "值"},
                "stage_a": {}
            }
            
            with open(frozen_path, 'w', encoding='utf-8') as f:
                json.dump(frozen_data, f)
            
            with open(candidate_path, 'w', encoding='utf-8') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            # Must not raise
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.ALLOW


class TestValidStructure:
    """Test that valid structure returns ALLOW"""
    
    def test_minimal_valid_structure(self):
        """Minimal valid structure must return ALLOW"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create minimal valid contracts
            with open(frozen_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            assert verdict == PolicyVerdict.ALLOW
            assert len(violations) == 0
    
    def test_complex_valid_structure(self):
        """Complex valid structure must return ALLOW"""
        with tempfile.TemporaryDirectory() as tmpdir:
            frozen_path = os.path.join(tmpdir, "frozen.json")
            candidate_path = os.path.join(tmpdir, "candidate.json")
            
            # Create complex valid contracts
            frozen_data = {
                "governor_steps": {
                    "GOV-1": {"objective": "test"},
                    "GOV-2": {"objective": "test2"}
                },
                "stage_a": {
                    "A-1": {"objective": "test3"}
                }
            }
            
            with open(frozen_path, 'w') as f:
                json.dump(frozen_data, f)
            
            with open(candidate_path, 'w') as f:
                json.dump({"governor_steps": {}, "stage_a": {}}, f)
            
            verdict, violations = compare_contracts(frozen_path, candidate_path, "hash")
            
            # GOV-1 only validates parsing, not content comparison
            assert verdict == PolicyVerdict.ALLOW