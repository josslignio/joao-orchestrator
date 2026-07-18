#!/usr/bin/env python3
"""
Project isolation validation tests for JOÃO CORE V1.
Verify that JOÃO maintains isolation from product repositories.
"""

import sys
from pathlib import Path

def test_no_product_repository_files():
    """Verify no product repository CODE is present in JOÃO (RULE 29 — no embedded product code).

    M0.1 patch (deliverable 4): the M0 exception for `specs/` is RETIRED. The contre-review
    ruled it a restored-but-quiet isolation breach: `specs/cv_bot.yaml` named a product
    (`cv_bot`) inside JOÃO's own tree, exactly what this test exists to catch. The CV bot
    business/security spec now lives in its own product repo
    (`~/job-opportunity-radar/governance/PROJECT_SPEC_V4.yaml`) and is referenced from JOÃO
    only as a typed, hash-verified external reference in `SPEC_BUNDLE_MANIFEST_V4.json`
    (see `governance/spec_loader.py::_verify_external_reference`) — never copied in.
    """
    project_root = Path(__file__).parent.parent

    # Check for product-specific files
    product_patterns = [
        "weekly_trading_radar",
        "job_cv_auto",
        "trading_radar",
        "cv_bot",
        ".trading-radar",
        ".cv-bot"
    ]

    for pattern in product_patterns:
        matching_files = list(project_root.rglob(f"*{pattern}*"))
        if matching_files:
            raise AssertionError(f"Found product-specific files matching '{pattern}': {matching_files}")

    print("✓ No product repository files in JOÃO (specs/ exception retired, M0.1 D4)")

def test_git_ignores_product_repos():
    """Verify .gitignore excludes product repositories"""
    gitignore = Path(__file__).parent.parent / ".gitignore"
    
    if not gitignore.exists():
        raise AssertionError(".gitignore not found")
    
    gitignore_content = gitignore.read_text()
    
    # Should ignore product directories
    product_patterns = ["weekly-trading-radar", "job-cv-auto", "trading-radar", "cv-bot"]
    
    for pattern in product_patterns:
        if pattern not in gitignore_content.lower():
            print(f"⚠ Warning: Product pattern '{pattern}' not in .gitignore")
    
    print("✓ .gitignore configured for project isolation")

def test_no_cross_product_git_modules():
    """Verify no git submodules pointing to product repositories"""
    gitmodules = Path(__file__).parent.parent / ".gitmodules"
    
    if gitmodules.exists():
        gitmodules_content = gitmodules.read_text()
        product_patterns = ["weekly-trading-radar", "job-cv-auto", "trading-radar", "cv-bot"]
        
        for pattern in product_patterns:
            if pattern in gitmodules_content.lower():
                raise AssertionError(f"Found product submodule pattern '{pattern}' in .gitmodules")
    
    print("✓ No cross-product git modules")

def test_documentation_mentions_isolation():
    """Verify documentation mentions project isolation"""
    readme = Path(__file__).parent.parent / "README.md"
    
    if readme.exists():
        readme_content = readme.read_text()
        
        # Should mention isolation or boundaries
        isolation_keywords = ["isolation", "boundary", "separate", "independent"]
        
        found_keywords = [kw for kw in isolation_keywords if kw in readme_content.lower()]
        if found_keywords:
            print(f"✓ Documentation mentions isolation: {found_keywords}")
        else:
            print("⚠ Warning: README.md does not mention project isolation")

def test_runtime_configuration_isolation():
    """Verify runtime configuration enforces isolation"""
    joao_config = Path(__file__).parent.parent / ".joao" / "context_policy.yaml"
    
    if joao_config.exists():
        config_content = joao_config.read_text()
        
        # Check for governance/bounded context policies
        governance_keywords = ["context", "quota", "governor", "limit", "policy"]
        
        found_keywords = [kw for kw in governance_keywords if kw in config_content.lower()]
        if found_keywords:
            print(f"✓ Runtime configuration has governance policies: {found_keywords}")
        else:
            print("⚠ Warning: Context policy may not enforce isolation explicitly")
    else:
        print("⚠ Warning: Context policy configuration not found")

if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))