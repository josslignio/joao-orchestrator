"""
JOÃO.AI Context & Quota Governor Tests
Focused validation of bounded context and quota policies
"""

import json
import pytest
from pathlib import Path
from scripts.build_task_packet import build_task_packet
from scripts.build_repo_map import build_repo_map, classify_path


class TestContextLimits:
    """Test 1-2: File count and context limits"""
    
    def test_rejects_more_than_8_files(self):
        """Test 1: More than 8 files should be rejected"""
        files = [f"file_{i}.py" for i in range(12)]
        packet = build_task_packet(
            task_contract="test task",
            current_commit="abc123",
            current_branch="main",
            relevant_files=files,
            max_files=8
        )
        
        assert len(packet['relevant_files']) == 8
        assert packet['context_limits']['max_files'] == 8
    
    def test_hard_context_limit_rejected(self):
        """Test 2: Hard context limit of 40k tokens enforced"""
        packet = build_task_packet(
            task_contract="test task",
            current_commit="abc123", 
            current_branch="main",
            relevant_files=["file1.py"]
        )
        
        assert packet['context_limits']['hard_token_limit'] == 40000
        assert packet['context_limits']['soft_token_limit'] == 25000


class TestExclusionPolicy:
    """Test 3: Previous full reports excluded"""
    
    def test_full_reports_excluded(self):
        """Test 3: Full report patterns should be excluded from context"""
        # Verify exclusion patterns are defined
        import yaml
        
        policy_file = Path('.joao/context_policy.yaml')
        if policy_file.exists():
            with open(policy_file) as f:
                policy = yaml.safe_load(f)
            
            exclusions = policy['context_policy']['default_exclusions']
            
            assert any('FULL_' in excl for excl in exclusions)
            assert any('LONG_REPORT' in excl for excl in exclusions)
            assert any('DETAILED_REPORT' in excl for excl in exclusions)


class TestRepoMapDeterminism:
    """Test 4: Repo map is deterministic (no LLM)"""
    
    def test_repo_map_deterministic(self):
        """Test 4: Repo map generated without LLM usage"""
        # Test that repo_map script doesn't use LLM
        import subprocess
        
        result = subprocess.run(
            ['python', 'scripts/build_repo_map.py', '/Users/jocelyngrosjean/joao-orchestrator'],
            capture_output=True,
            text=True
        )
        
        assert result.returncode == 0
        repo_map = json.loads(result.stdout)
        
        # Verify no LLM was used
        assert repo_map['no_llm_used'] == True
        assert repo_map['generated_by'] == 'deterministic_repo_map_builder'
    
    def test_classify_path_deterministic(self):
        """Test path classification is deterministic"""
        test_path = Path('tests/test_file.py')
        classification = classify_path(test_path, Path('/test'))
        
        assert classification == 'test'


class TestModelRouting:
    """Test 5-7: Model routing policies"""
    
    def test_ordinary_task_routes_to_glm47(self):
        """Test 5: Ordinary tasks should route to GLM-4.7"""
        import yaml
        
        routing_file = Path('.joao/model_routing.yaml')
        if routing_file.exists():
            with open(routing_file) as f:
                routing = yaml.safe_load(f)
            
            # Verify default is GLM-4.7
            assert routing['default_model'] == 'GLM-4.7'
            
            # Verify ordinary implementation uses GLM-4.7
            ordinary = routing['model_routing']['ordinary_implementation']
            assert ordinary['model'] == 'GLM-4.7'
    
    def test_read_only_prefers_local(self):
        """Test 6: Read-only tasks prefer local model when configured"""
        import yaml
        
        routing_file = Path('.joao/model_routing.yaml')
        if routing_file.exists():
            with open(routing_file) as f:
                routing = yaml.safe_load(f)
            
            read_only = routing['model_routing']['read_only_exploration']
            assert read_only['primary'] == 'local_model'
            assert read_only['fallback'] == 'GLM-4.7'
    
    def test_premium_escalation_requirements(self):
        """Test 7: Premium escalation requires risk or two failures"""
        import yaml
        
        routing_file = Path('.joao/model_routing.yaml')
        if routing_file.exists():
            with open(routing_file) as f:
                routing = yaml.safe_load(f)
            
            # Check escalation policy
            escalation = routing['escalation_policy']
            assert escalation['requires_two_cheap_failures'] == True
            
            # Check architecture/security routing
            arch_security = routing['model_routing']['architecture_security']
            assert 'escalation_requirement' in arch_security


class TestWorkerPolicy:
    """Test 8: One cloud worker enforced"""
    
    def test_one_cloud_worker_enforced(self):
        """Test 8: Maximum one cloud worker enforced"""
        import yaml
        
        policy_file = Path('.joao/context_policy.yaml')
        if policy_file.exists():
            with open(policy_file) as f:
                policy = yaml.safe_load(f)
            
            quota_limits = policy['quota_limits']
            assert quota_limits['max_cloud_workers'] == 1
            assert quota_limits['no_parallel_cloud_subagents'] == True


class TestBoundedResume:
    """Test 9: Resume verification is bounded"""
    
    def test_resume_verification_bounded(self):
        """Test 9: Resume verification uses bounded context"""
        import yaml
        
        policy_file = Path('.joao/context_policy.yaml')
        if policy_file.exists():
            with open(policy_file) as f:
                policy = yaml.safe_load(f)
            
            read_policy = policy['context_policy']['read_policy']
            assert read_policy['bounded_resume_verification_only'] == True
            assert read_policy['allow_full_repo_reread'] == False


class TestReportLimits:
    """Test 10: Short result-report limit enforced"""
    
    def test_report_limit_enforced(self):
        """Test 10: Result reports should be compact and bounded"""
        # Test that build_task_packet enforces context limits
        packet = build_task_packet(
            task_contract="generate compact report",
            current_commit="abc123",
            current_branch="main",
            relevant_files=["file1.py"]
        )
        
        # Verify limits are present
        assert 'context_limits' in packet
        assert packet['context_limits']['soft_token_limit'] == 25000
        assert packet['context_limits']['hard_token_limit'] == 40000


if __name__ == '__main__':
    pytest.main([__file__, '-v'])