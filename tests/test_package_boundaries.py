#!/usr/bin/env python3
"""
Package boundary validation tests for JOÃO CORE V1.
Verify that joao-orchestrator maintains proper package boundaries.
"""

import sys
from pathlib import Path

# Add src to path for imports
src_path = Path(__file__).parent.parent / "src"
if src_path.exists():
    sys.path.insert(0, str(src_path))

def test_joao_package_exists():
    """Verify joao_orchestrator package exists and is importable"""
    try:
        import joao_orchestrator
        assert hasattr(joao_orchestrator, '__version__')
        print(f"✓ joao_orchestrator package exists (version {joao_orchestrator.__version__})")
    except ImportError as e:
        raise AssertionError(f"joao_orchestrator package not found: {e}")

def test_joss_compatibility_shim_exists():
    """Verify joss_orchestrator compatibility shim exists"""
    try:
        import joss_orchestrator
        print("✓ joss_orchestrator compatibility shim exists")
    except ImportError as e:
        raise AssertionError(f"joss_orchestrator compatibility shim not found: {e}")

def test_no_cross_package_imports_from_joao():
    """Verify joao_orchestrator does not import from product packages"""
    joao_src = Path(__file__).parent.parent / "src" / "joao_orchestrator"
    
    # Check for actual disallowed cross-package imports (not string mentions)
    disallowed_imports = [
        'from weekly_trading_radar',
        'from job_cv_auto', 
        'import weekly_trading_radar',
        'import job_cv_auto'
    ]
    
    for py_file in joao_src.rglob("*.py"):
        content = py_file.read_text()
        for import_pattern in disallowed_imports:
            if import_pattern in content:
                raise AssertionError(f"Found disallowed import '{import_pattern}' in {py_file}")
    
    print("✓ No cross-package imports from joao_orchestrator")

def test_no_concrete_product_identifiers_in_canonical_package():
    """Concrete product presets and datasets must remain outside ``src``."""
    joao_src = Path(__file__).parent.parent / "src" / "joao_orchestrator"
    forbidden_identifiers = [
        "weekly-trading-radar",
        "job-opportunity-radar",
        "job_cv_auto",
        "weekly_trading_radar",
        "twitter-scrape-test",
        "twscrape",
        "joss.tradingradar",
        "weekly_v1_delivery.py",
    ]
    violations = []
    for py_file in joao_src.rglob("*.py"):
        content = py_file.read_text().lower()
        for identifier in forbidden_identifiers:
            if identifier in content:
                violations.append(f"{py_file}: {identifier}")
    assert not violations, (
        "Concrete product identifiers belong under project_profiles/, not "
        f"src/joao_orchestrator: {violations}"
    )

def test_joao_core_modules_boundaries():
    """Verify core JOÃO modules maintain proper boundaries"""
    core_modules = [
        "joao_orchestrator.v2.pr_gates",
        "joao_orchestrator.v2.gate_ledger", 
        "joao_orchestrator.v2.autonomy",
        "joao_orchestrator.v2.governance",
        "joao_orchestrator.v2.state"
    ]
    
    for module_name in core_modules:
        try:
            module = __import__(module_name, fromlist=[''])
            assert module is not None
        except ImportError as e:
            raise AssertionError(f"Core module '{module_name}' not importable: {e}")
    
    print("✓ All core JOÃO modules maintain boundaries")

def test_package_structure_integrity():
    """Verify package structure is maintained"""
    joao_root = Path(__file__).parent.parent / "src" / "joao_orchestrator"
    
    # Verify key directories exist
    required_dirs = ["v2", "optimization"]
    for dir_name in required_dirs:
        dir_path = joao_root / dir_name
        if not dir_path.exists():
            raise AssertionError(f"Required directory '{dir_name}' not found in joao_orchestrator")
    
    print("✓ Package structure integrity maintained")

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
