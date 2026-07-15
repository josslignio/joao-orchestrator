#!/usr/bin/env python3
"""
JOÃO.AI Deterministic Repository Map Builder
Generates repository structure without LLM usage
"""

import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set


def extract_module_info(file_path: Path) -> Dict[str, Any]:
    """Extract module information from Python source file"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            source = f.read()
        tree = ast.parse(source)
        
        module_docstring = ast.get_docstring(tree)
        
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    for alias in node.names:
                        imports.append(f"{node.module}.{alias.name}")
        
        functions = []
        classes = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                functions.append(node.name)
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
        
        return {
            "docstring": module_docstring,
            "imports": imports,
            "functions": functions,
            "classes": classes,
            "has_entry_point": len([f for f in functions if f in ['main', 'run', 'start']]) > 0
        }
    except Exception as e:
        return {
            "error": str(e),
            "docstring": None,
            "imports": [],
            "functions": [],
            "classes": [],
            "has_entry_point": False
        }


def classify_path(path: Path, base_dir: Path) -> str:
    """Classify file/directory purpose"""
    path_str = str(path.relative_to(base_dir))
    
    if 'test' in path_str.lower():
        return 'test'
    elif 'script' in path_str.lower():
        return 'script'
    elif 'doc' in path_str.lower():
        return 'documentation'
    elif path.suffix == '.py':
        return 'python_module'
    elif path.suffix in ['.md', '.rst']:
        return 'documentation'
    elif path.suffix in ['.json', '.yaml', '.yml']:
        return 'config'
    else:
        return 'other'


def build_repo_map(root_dir: Path, max_depth: int = 5) -> Dict[str, Any]:
    """
    Build deterministic repository map without LLM
    
    Args:
        root_dir: Repository root directory
        max_depth: Maximum directory depth to explore
    """
    python_files = list(root_dir.rglob('*.py'))
    
    modules = []
    for py_file in python_files:
        rel_path = py_file.relative_to(root_dir)
        
        # Skip test files and __pycache__
        if 'test' in rel_path.parts or '__pycache__' in rel_path.parts:
            continue
            
        module_info = extract_module_info(py_file)
        module_info['path'] = str(rel_path)
        module_info['classification'] = classify_path(py_file, root_dir)
        modules.append(module_info)
    
    # Determine owner/product from structure
    owner_product = determine_owner_product(root_dir, modules)
    
    # Identify immutable/frozen modules
    frozen_modules = identify_frozen_modules(root_dir, modules)
    
    return {
        "repository_root": str(root_dir),
        "total_modules": len(modules),
        "modules": modules[:50],  # Limit for practical use
        "owner_product": owner_product,
        "frozen_modules": frozen_modules,
        "entry_points": [m['path'] for m in modules if m.get('has_entry_point')],
        "generated_by": "deterministic_repo_map_builder",
        "no_llm_used": True
    }


def determine_owner_product(root_dir: Path, modules: List[Dict]) -> Dict[str, str]:
    """Determine owner and product from repository structure"""
    # Check for common indicators
    setup_files = ['pyproject.toml', 'setup.py', 'setup.cfg']
    project_name = "unknown"
    owner = "unknown"
    
    for setup_file in setup_files:
        setup_path = root_dir / setup_file
        if setup_path.exists():
            try:
                if setup_file == 'pyproject.toml':
                    import tomllib
                    with open(setup_path, 'rb') as f:
                        data = tomllib.load(f)
                        project_name = data.get('project', {}).get('name', 'unknown')
                        # Try to derive owner from project metadata
                        owner = data.get('tool', {}).get('poetry', {}).get('authors', ["unknown"])[0] if data.get('tool', {}).get('poetry') else "unknown"
                break
            except:
                pass
    
    # Try git remote to determine owner as fallback
    if owner == "unknown":
        try:
            import subprocess
            result = subprocess.run(['git', 'config', '--get', 'remote.origin.url'], 
                                  capture_output=True, text=True, cwd=root_dir)
            if result.returncode == 0 and result.stdout:
                # Extract owner from git URL (e.g., git@github.com:owner/repo.git)
                git_url = result.stdout.strip()
                if 'github.com' in git_url or 'git@github.com' in git_url:
                    parts = git_url.split('/')[-1].replace('.git', '')
                    owner_part = git_url.split('/')[-2].split(':')[-1] if ':' in git_url else git_url.split('/')[-2]
                    owner = owner_part
        except:
            pass
    
    return {
        "owner": owner,
        "product": project_name,
        "determination_method": "static_analysis"
    }


def identify_frozen_modules(root_dir: Path, modules: List[Dict]) -> List[str]:
    """Identify modules that should be considered frozen/immutable"""
    frozen_patterns = ['legacy', 'compat', 'shim', 'deprecated']
    frozen = []
    
    for module in modules:
        path = module.get('path', '')
        if any(pattern in path.lower() for pattern in frozen_patterns):
            frozen.append(path)
    
    return frozen


def main():
    """CLI entry point"""
    if len(sys.argv) < 2:
        print("Usage: build_repo_map.py <repository_root>", file=sys.stderr)
        sys.exit(1)
    
    root_dir = Path(sys.argv[1])
    if not root_dir.exists():
        print(f"Error: Directory {root_dir} does not exist", file=sys.stderr)
        sys.exit(1)
    
    repo_map = build_repo_map(root_dir)
    print(json.dumps(repo_map, indent=2))


if __name__ == "__main__":
    main()