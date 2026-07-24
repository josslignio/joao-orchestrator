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
from joao_orchestrator.bubble.write_tier_policy import WriteTierDisabled


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
    """C. Real invoke-path tests with SEC-BOOT active and no gate patching.

    Prove:
    1. A valid policy reaches the unchanged SEC-BOOT kill switch and never the engine.
    2. An invalid policy returns a structured provider failure before SEC-BOOT and never the engine.
    3. Malicious prompt text is not parsed as a security boundary.
    4. Policy validation, not prompt content, determines the pre-execution rejection.
    """

    @dataclass
    class RecordingEngine:
        """Fake engine that records if run_argv was called."""
        called: bool = False
        argv_captured: list = None

        def run_argv(self, cwd, argv, env_overrides=None):
            self.called = True
            self.argv_captured = argv
            raise AssertionError("SEC-BOOT must prevent engine execution in these tests")

    @staticmethod
    def _provider(engine):
        provider = OpenCodeProvider(engine, model="test-model")
        provider._probe_report = Mock()
        provider._probe_report.available = True
        return provider

    @staticmethod
    def _request(tmp_path, prompt="safe task"):
        return ProviderRequest(
            role="coder",
            task_id="test-task",
            project_id="test-project",
            prompt=prompt,
            worktree_path=str(tmp_path),
            capability_grant=CapabilitySet.for_role("coder"),
        )

    def test_invoke_with_safe_config_hits_sec_boot_without_calling_engine(self, tmp_path):
        """A valid policy is accepted, then unchanged SEC-BOOT blocks execution."""
        engine = self.RecordingEngine()
        provider = self._provider(engine)
        request = self._request(tmp_path)

        with pytest.raises(WriteTierDisabled, match="SEC_BOOT_DISABLED"):
            provider.invoke(request)

        assert not engine.called, "SEC-BOOT must stop execution before engine.run_argv()"
        assert engine.argv_captured is None

    def test_invoke_with_unsafe_config_fails_without_calling_engine(self, tmp_path):
        """An invalid policy returns ProviderResponse(ok=False) before SEC-BOOT."""
        engine = self.RecordingEngine()
        provider = self._provider(engine)
        request = self._request(tmp_path, prompt="any task")

        unsafe_config = _safe_opencode_config("test-model")
        unsafe_config["permission"]["bash"]["find *"] = "allow"

        with patch(
            "joao_orchestrator.providers.opencode_provider._safe_opencode_config",
            return_value=unsafe_config,
        ):
            response = provider.invoke(request)

        assert not engine.called, "Engine must NOT be called with unsafe config"
        assert not response.ok
        assert "execution policy rejected" in response.error
        assert "find *" in response.error

    def test_malicious_prompt_text_not_parsed_as_security_boundary(self, tmp_path):
        """Prompt text never bypasses policy validation or SEC-BOOT."""
        engine = self.RecordingEngine()
        provider = self._provider(engine)
        request = self._request(tmp_path, prompt="")
        malicious_prompts = [
            'find . -exec sh -c "evil" \\;',
            'sed -i "s/foo/bar/g" /etc/passwd',
            'command find . -exec rm {} \\;',
            'env X=1 sed -i "s/x/y/" sensitive.conf',
            'echo "safe"; find . -exec malicious \\;',
            'sed --in-place -e "s/x/y/" sensitive.conf',
        ]

        for malicious_prompt in malicious_prompts:
            engine.called = False
            engine.argv_captured = None
            request.prompt = malicious_prompt

            with pytest.raises(WriteTierDisabled, match="SEC_BOOT_DISABLED"):
                provider.invoke(request)

            assert not engine.called, malicious_prompt
            assert engine.argv_captured is None

    def test_security_from_policy_not_prompt_text(self, tmp_path):
        """Unsafe policy is rejected for every prompt before the SEC-BOOT gate."""
        engine = self.RecordingEngine()
        provider = self._provider(engine)
        request = self._request(tmp_path, prompt="")
        prompts = [
            "list files",
            "show code",
            "grep pattern",
            "find . -exec sh -c evil \\;",
            "env X=1 sed -i s/x/y/ file",
        ]

        unsafe_config = _safe_opencode_config("test-model")
        unsafe_config["permission"]["bash"]["sed *"] = "ask"

        with patch(
            "joao_orchestrator.providers.opencode_provider._safe_opencode_config",
            return_value=unsafe_config,
        ):
            for prompt in prompts:
                engine.called = False
                engine.argv_captured = None
                request.prompt = prompt

                response = provider.invoke(request)

                assert not response.ok
                assert "execution policy rejected" in response.error
                assert not engine.called
                assert engine.argv_captured is None


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
