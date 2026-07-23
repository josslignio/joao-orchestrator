"""M2_HARD_CLOSE: OpenCode Provider Security Policy Tests

Tests for the production execution policy validator that ensures find/sed
remain completely denied in OpenCode's actual bash permission policy.

These tests prove:
1. The real safe config includes all required deny patterns
2. The validator rejects mutations of any security condition
3. The validator runs in the real invoke() path before engine execution
4. No prompt scanning or fake argv parsing exists (dead code test)
5. SEC-BOOT behavior remains unchanged
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from dataclasses import dataclass

from joao_orchestrator.providers.opencode_provider import (
    OpenCodeProvider,
    _safe_opencode_config,
    _validate_opencode_execution_policy,
)
from joao_orchestrator.providers.base import ProviderRequest, ProviderCapabilityError
from joao_orchestrator.policy.capabilities import CapabilitySet


class TestStaticPolicyDenies:
    """A. Static policy tests: Assert the real safe config includes every required deny pattern."""

    def test_safe_config_wildcard_deny(self):
        """permission[*] must be 'deny'"""
        config = _safe_opencode_config("test-model")
        assert config["permission"]["*"] == "deny"

    def test_safe_config_external_directory_deny(self):
        """permission[external_directory] must be 'deny'"""
        config = _safe_opencode_config("test-model")
        assert config["permission"]["external_directory"] == "deny"

    def test_safe_config_webfetch_deny(self):
        """permission[webfetch] must be 'deny'"""
        config = _safe_opencode_config("test-model")
        assert config["permission"]["webfetch"] == "deny"

    def test_safe_config_websearch_deny(self):
        """permission[websearch] must be 'deny'"""
        config = _safe_opencode_config("test-model")
        assert config["permission"]["websearch"] == "deny"

    def test_safe_config_bash_wildcard_deny(self):
        """permission[bash][*] must be 'deny'"""
        config = _safe_opencode_config("test-model")
        assert config["permission"]["bash"]["*"] == "deny"

    def test_safe_config_find_denies(self):
        """All find variants must be 'deny'"""
        config = _safe_opencode_config("test-model")
        bash = config["permission"]["bash"]
        
        find_patterns = [
            "find *",
            "gfind *",
            "/bin/find *",
            "/usr/bin/find *",
            "/usr/local/bin/find *",
            "command find *",
            "command gfind *",
            "env * find *",
            "env * gfind *",
        ]
        
        for pattern in find_patterns:
            assert bash.get(pattern) == "deny", f"find pattern '{pattern}' must be 'deny'"

    def test_safe_config_sed_denies(self):
        """All sed variants must be 'deny'"""
        config = _safe_opencode_config("test-model")
        bash = config["permission"]["bash"]
        
        sed_patterns = [
            "sed *",
            "gsed *",
            "/bin/sed *",
            "/usr/bin/sed *",
            "/usr/local/bin/sed *",
            "command sed *",
            "command gsed *",
            "env * sed *",
            "env * gsed *",
        ]
        
        for pattern in sed_patterns:
            assert bash.get(pattern) == "deny", f"sed pattern '{pattern}' must be 'deny'"

    def test_safe_config_secret_file_denies(self):
        """Secret file patterns in read section must be 'deny'"""
        config = _safe_opencode_config("test-model")
        read = config["permission"]["read"]
        
        secret_patterns = ["*.env", "*.env.*", "*.pem", "*.key"]
        
        for pattern in secret_patterns:
            assert read.get(pattern) == "deny", f"secret pattern '{pattern}' must be 'deny'"

    def test_safe_config_legitimate_capabilities_unchanged(self):
        """Legitimate non-shell capabilities intended by M1 remain unchanged."""
        config = _safe_opencode_config("test-model")
        permission = config["permission"]
        
        # Read capabilities
        assert permission["read"]["*"] == "allow"
        assert permission["glob"] == "allow"
        assert permission["grep"] == "allow"
        assert permission["lsp"] == "allow"
        
        # Bash allow-list for safe inspection
        bash = permission["bash"]
        assert bash["pwd"] == "allow"
        assert bash["ls*"] == "allow"
        assert bash["git status*"] == "allow"
        assert bash["git ls-files*"] == "allow"
        assert bash["git rev-parse*"] == "allow"
        assert bash["python -m compileall*"] == "allow"
        assert bash["python3 -m compileall*"] == "allow"


class TestMutationMatrix:
    """B. Mutation matrix: Starting from real safe config, mutate one condition at a time.
    
    Validator must reject each mutation.
    """

    def get_safe_config(self):
        """Get a real safe config to mutate."""
        return _safe_opencode_config("test-model")

    def test_reject_bash_wildcard_becomes_allow(self):
        """bash wildcard becomes 'allow' must be rejected"""
        config = self.get_safe_config()
        config["permission"]["bash"]["*"] = "allow"
        
        with pytest.raises(PermissionError, match="bash\\['\\*'\\] must be 'deny'"):
            _validate_opencode_execution_policy(config)

    def test_reject_find_allow_mutation(self):
        """find * becomes 'allow' must be rejected"""
        config = self.get_safe_config()
        config["permission"]["bash"]["find *"] = "allow"
        
        with pytest.raises(PermissionError, match="bash\\['find \\*'\\] must be 'deny'"):
            _validate_opencode_execution_policy(config)

    def test_reject_sed_allow_mutation(self):
        """sed * becomes 'allow' must be rejected"""
        config = self.get_safe_config()
        config["permission"]["bash"]["sed *"] = "allow"
        
        with pytest.raises(PermissionError, match="bash\\['sed \\*'\\] must be 'deny'"):
            _validate_opencode_execution_policy(config)

    def test_reject_usr_bin_find_missing(self):
        """/usr/bin/find * missing must be rejected"""
        config = self.get_safe_config()
        del config["permission"]["bash"]["/usr/bin/find *"]
        
        with pytest.raises(PermissionError, match="bash\\['/usr/bin/find \\*'\\] must be 'deny'"):
            _validate_opencode_execution_policy(config)

    def test_reject_bin_sed_missing(self):
        """/bin/sed * missing must be rejected"""
        config = self.get_safe_config()
        del config["permission"]["bash"]["/bin/sed *"]
        
        with pytest.raises(PermissionError, match="bash\\['/bin/sed \\*'\\] must be 'deny'"):
            _validate_opencode_execution_policy(config)

    def test_reject_external_directory_allow_mutation(self):
        """external_directory becomes 'allow' must be rejected"""
        config = self.get_safe_config()
        config["permission"]["external_directory"] = "allow"
        
        with pytest.raises(PermissionError, match="permission\\['external_directory'\\] must be 'deny'"):
            _validate_opencode_execution_policy(config)

    def test_reject_webfetch_allow_mutation(self):
        """webfetch becomes 'allow' must be rejected"""
        config = self.get_safe_config()
        config["permission"]["webfetch"] = "allow"
        
        with pytest.raises(PermissionError, match="permission\\['webfetch'\\] must be 'deny'"):
            _validate_opencode_execution_policy(config)

    def test_reject_bash_section_missing(self):
        """bash section missing must be rejected"""
        config = self.get_safe_config()
        del config["permission"]["bash"]
        
        with pytest.raises(PermissionError, match="missing required 'bash' section"):
            _validate_opencode_execution_policy(config)

    def test_reject_permission_section_malformed(self):
        """permission section malformed (not a dict) must be rejected"""
        config = self.get_safe_config()
        config["permission"] = "not-a-dict"
        
        with pytest.raises(PermissionError, match="'permission' section must be a dictionary"):
            _validate_opencode_execution_policy(config)

    def test_reject_config_not_dict(self):
        """Config not a dictionary must be rejected"""
        with pytest.raises(PermissionError, match="config must be a dictionary"):
            _validate_opencode_execution_policy("not-a-dict")

    def test_reject_permission_section_missing(self):
        """permission section missing must be rejected"""
        config = {"$schema": "test"}  # No permission section
        with pytest.raises(PermissionError, match="missing required 'permission' section"):
            _validate_opencode_execution_policy(config)

    def test_reject_wildcard_allow_mutation(self):
        """permission wildcard becomes 'allow' must be rejected"""
        config = self.get_safe_config()
        config["permission"]["*"] = "allow"
        
        with pytest.raises(PermissionError, match="permission\\['\\*'\\] must be 'deny'"):
            _validate_opencode_execution_policy(config)


class TestRealInvokePath:
    """C. Real invoke-path tests: Use real OpenCodeProvider.invoke() with recording/fake engine.
    
    Prove:
    1. With unmodified safe config, engine is called.
    2. When _safe_opencode_config() returns unsafe policy, invoke() fails and engine never called.
    3. Malicious prompt text is not parsed as security boundary.
    4. Security comes from execution policy, not prompt text.
    """

    @dataclass
    class RecordingEngine:
        """Fake engine that records if run_argv was called."""
        called: bool = False
        argv_captured: list = None
        
        def run_argv(self, cwd, argv, env_overrides=None):
            self.called = True
            self.argv_captured = argv
            # Return a mock successful result
            from joao_orchestrator.providers.subprocess_cli import CLIExecutionResult
            return CLIExecutionResult(
                engine_name="test",
                argv=argv,
                cwd=str(cwd),
                returncode=0,
                stdout="success",
                stderr="",
                started_at="2024-01-01T00:00:00",
                finished_at="2024-01-01T00:00:01",
                duration_seconds=1.0,
            )

    def test_invoke_with_safe_config_calls_engine(self, tmp_path):
        """With unmodified safe config, the engine is called."""
        engine = self.RecordingEngine()
        provider = OpenCodeProvider(engine, model="test-model")
        
        # Mock probe to return available
        provider._probe_report = Mock()
        provider._probe_report.available = True
        
        request = ProviderRequest(
            role="coder",
            task_id="test-task",
            project_id="test-project",
            prompt="safe task",
            worktree_path=str(tmp_path),
            capability_grant=CapabilitySet.for_role("coder"),
        )
        
        # Mock SEC-BOOT to exercise post-gate production path
        with patch('joao_orchestrator.bubble.write_tier_policy.assert_write_tier_enabled'):
            response = provider.invoke(request)
        
        assert engine.called, "Engine must be called with safe config"
        assert response.ok, "Response must be ok"
        assert engine.argv_captured is not None, "argv must be captured"

    def test_invoke_with_unsafe_config_fails_without_calling_engine(self, tmp_path):
        """When _safe_opencode_config() returns unsafe policy, invoke() fails and engine never called."""
        engine = self.RecordingEngine()
        provider = OpenCodeProvider(engine, model="test-model")
        
        # Mock probe to return available
        provider._probe_report = Mock()
        provider._probe_report.available = True
        
        request = ProviderRequest(
            role="coder",
            task_id="test-task",
            project_id="test-project",
            worktree_path=str(tmp_path),
            prompt="any task",
            capability_grant=CapabilitySet.for_role("coder"),
        )
        
        # Monkeypatch _safe_opencode_config to return unsafe policy
        unsafe_config = _safe_opencode_config("test-model")
        unsafe_config["permission"]["bash"]["find *"] = "allow"  # UNSAFE MUTATION
        
        # Mock SEC-BOOT to exercise post-gate production path
        with patch('joao_orchestrator.bubble.write_tier_policy.assert_write_tier_enabled'):
            with patch('joao_orchestrator.providers.opencode_provider._safe_opencode_config', return_value=unsafe_config):
                response = provider.invoke(request)
        
        assert not engine.called, "Engine must NOT be called with unsafe config"
        assert not response.ok, "Response must fail"
        assert "execution policy rejected" in response.error, "Error must mention policy rejection"

    def test_malicious_prompt_text_not_parsed_as_security_boundary(self, tmp_path):
        """Malicious prompt containing find -exec, sed -i, newlines, quoting is NOT parsed as security boundary."""
        engine = self.RecordingEngine()
        provider = OpenCodeProvider(engine, model="test-model")
        
        # Mock probe to return available
        provider._probe_report = Mock()
        provider._probe_report.available = True
        
        # Malicious prompts with various obfuscations
        malicious_prompts = [
            'find . -exec sh -c "evil" \\;',
            'sed -i "s/foo/bar/g" /etc/passwd',
            'find /etc -name "passwd" -exec cat {} \\;',
            'echo "safe"; find . -exec malicious \\;',
            'sed --in-place -e "s/x/y/" sensitive.conf',
            'find . -type f -exec rm {} \\;',
        ]
        
        request = ProviderRequest(
            role="coder",
            task_id="test-task",
            project_id="test-project",
            worktree_path=str(tmp_path),
            prompt="",
            capability_grant=CapabilitySet.for_role("coder"),
        )
        
        # Mock SEC-BOOT to exercise post-gate production path
        with patch('joao_orchestrator.bubble.write_tier_policy.assert_write_tier_enabled'):
            for malicious_prompt in malicious_prompts:
                # Reset engine state
                engine.called = False
                
                request.prompt = malicious_prompt
                
                response = provider.invoke(request)
                
                # The key assertion: prompt text does NOT control security
                # Security comes from the policy, not from parsing the prompt
                assert response.ok, f"Prompt must not control security: {malicious_prompt}"
                assert engine.called, "Engine called because policy (not prompt) controls execution"

    def test_security_from_policy_not_prompt_text(self, tmp_path):
        """Security comes from the execution policy handed to OpenCode, not from prompt text."""
        engine = self.RecordingEngine()
        provider = OpenCodeProvider(engine, model="test-model")
        
        # Mock probe to return available
        provider._probe_report = Mock()
        provider._probe_report.available = True
        
        request = ProviderRequest(
            role="coder",
            task_id="test-task",
            project_id="test-project",
            worktree_path=str(tmp_path),
            prompt="",
            capability_grant=CapabilitySet.for_role("coder"),
        )
        
        # Test 1: Safe policy allows safe prompts
        safe_prompts = ["list files", "show code", "grep pattern"]
        
        # Mock SEC-BOOT to exercise post-gate production path
        with patch('joao_orchestrator.bubble.write_tier_policy.assert_write_tier_enabled'):
            for safe_prompt in safe_prompts:
                engine.called = False
                request.prompt = safe_prompt
                
                response = provider.invoke(request)
                
                assert response.ok, f"Safe prompt with safe policy: {safe_prompt}"
                assert engine.called, "Engine called"
        
        # Test 2: Unsafe policy blocks ALL prompts (even safe ones)
        unsafe_config = _safe_opencode_config("test-model")
        unsafe_config["permission"]["bash"]["find *"] = "allow"  # UNSAFE
        
        with patch('joao_orchestrator.bubble.write_tier_policy.assert_write_tier_enabled'):
            with patch('joao_orchestrator.providers.opencode_provider._safe_opencode_config', return_value=unsafe_config):
                for safe_prompt in safe_prompts:
                    engine.called = False
                    request.prompt = safe_prompt
                    
                    response = provider.invoke(request)
                    
                    assert not response.ok, "Unsafe policy blocks all prompts"
                    assert not engine.called, "Engine not called due to policy violation"


class TestNoDeadCode:
    """D. No-dead-code test: Assert source contains no prompt-regex command scanner and no _validate_command_argv."""

    def test_no_prompt_regex_scanning(self):
        """Source must not contain prompt regex scanning for commands."""
        import joao_orchestrator.providers.opencode_provider as op_module
        import inspect
        
        source = inspect.getsource(op_module)
        
        # Forbidden patterns that indicate prompt scanning
        forbidden_patterns = [
            "re.finditer",
            "re.search",
            "re.match",
            "re.compile.*find",
            "re.compile.*sed",
            "request.prompt.*find",
            "request.prompt.*sed",
            "prompt.*split.*find",
            "prompt.*split.*sed",
        ]
        
        for pattern in forbidden_patterns:
            # Check that pattern does not appear in source
            assert pattern not in source, f"Source must not contain prompt scanning: {pattern}"

    def test_no_validate_command_argv_function(self):
        """Source must not contain _validate_command_argv function."""
        import joao_orchestrator.providers.opencode_provider as op_module
        import inspect
        
        source = inspect.getsource(op_module)
        
        # The old fake mediation function must not exist
        assert "_validate_command_argv" not in source, "Source must not contain _validate_command_argv"
        
        # Verify function does not exist in module
        assert not hasattr(op_module, "_validate_command_argv"), "_validate_command_argv must not exist"

    def test_validator_name_is_correct(self):
        """The correct validator function must exist."""
        import joao_orchestrator.providers.opencode_provider as op_module
        
        # The correct validator must exist
        assert hasattr(op_module, "_validate_opencode_execution_policy"), \
            "_validate_opencode_execution_policy must exist"


class TestSecBootBehavior:
    """E. SEC-BOOT behavior: Keep write_tier_policy.py byte-identical."""
    
    M1_SEC_BOOT_HASH = "a0de9819151da3f8b8a2aa8a7630a282545503999e2d01d8ca237b3a6ddf88cd"
    
    def test_sec_boot_file_integrity(self):
        """write_tier_policy.py must remain byte-identical to M1."""
        import hashlib
        from pathlib import Path
        
        sec_boot_path = Path(__file__).parent.parent / "src" / "joao_orchestrator" / "bubble" / "write_tier_policy.py"
        
        # Calculate SHA256 of the current file
        with open(sec_boot_path, "rb") as f:
            current_hash = hashlib.sha256(f.read()).hexdigest()
        
        # Compare with M1 hash
        assert current_hash == self.M1_SEC_BOOT_HASH, \
            f"SEC-BOOT file changed: M1={self.M1_SEC_BOOT_HASH}, current={current_hash}"
