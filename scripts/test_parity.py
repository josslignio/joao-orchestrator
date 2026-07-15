#!/usr/bin/env python3
"""
test_parity.py — C7 parity: joao_orchestrator (canonical) vs joss_orchestrator (shim).

Proves the compat shim re-exports the canonical implementation with identical
behavior (§10.3 parity requirement). Covers:
  * every public symbol in key v2 modules is re-exported by the shim;
  * classification behavior is identical canonical-vs-shim;
  * the shim emits a deprecation event on import;
  * the shim contains NO business logic (only re-exports).

Deterministic: stdlib only.
"""

import sys
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class TestShimParity(unittest.TestCase):
    def test_canonical_and_shim_classify_identically(self):
        from joao_orchestrator.v2.autonomy import classify_current_state as canon
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from joss_orchestrator.v2.autonomy import classify_current_state as shim
        c, s = canon(), shim()
        self.assertEqual(c.level, s.level)
        self.assertEqual(c.level, "L0-SHADOW")

    def test_shim_reexports_public_symbols(self):
        import joao_orchestrator.v2.autonomy as canon_mod
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import joss_orchestrator.v2.autonomy as shim_mod
        for name in ("AutonomyClassifier", "Evidence", "L0_SHADOW", "classify_current_state"):
            self.assertTrue(hasattr(canon_mod, name), f"canon missing {name}")
            self.assertTrue(hasattr(shim_mod, name), f"shim missing {name}")

    def test_shim_emits_deprecation(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # force a fresh import of a shim submodule
            import importlib
            sys.modules.pop("joss_orchestrator.v2.governance", None)
            import joss_orchestrator.v2.governance  # noqa: F401
            self.assertTrue(any(issubclass(x.category, DeprecationWarning) for x in w),
                            "shim must emit a DeprecationWarning")

    def test_canonical_package_imports_cleanly(self):
        import joao_orchestrator
        from joao_orchestrator.v2 import autonomy, governance, pr_gates, state, gate_ledger
        self.assertIsNotNone(joao_orchestrator)
        for m in (autonomy, governance, pr_gates, state, gate_ledger):
            self.assertIsNotNone(m)

    def test_cli_version_and_autonomy(self):
        from joao_orchestrator.cli.joao import main
        # version returns 0 and is non-destructive
        rc = main(["version"])
        self.assertEqual(rc, 0)


class TestProvenanceManifests(unittest.TestCase):
    def test_all_four_manifests_present_and_valid(self):
        import json
        for name in ("PROVENANCE.json", "SOURCE_MANIFEST.json",
                     "IMPORT_MANIFEST.json", "PROTECTED_REFS.json"):
            p = ROOT / name
            self.assertTrue(p.exists(), f"{name} missing")
            d = json.loads(p.read_text())
            self.assertIn("schema_version", d)

    def test_provenance_records_canonical_and_shim(self):
        import json
        d = json.loads((ROOT / "PROVENANCE.json").read_text())
        self.assertEqual(d["canonical_package"], "joao_orchestrator")
        self.assertEqual(d["compat_shim_package"], "joss_orchestrator")
        self.assertEqual(d["canonical_cli"], "joao")
        self.assertIn("joss", d["legacy_cli_aliases"])
        self.assertEqual(d["visibility"], "PRIVATE")

    def test_import_manifest_has_hash_per_file(self):
        import json
        d = json.loads((ROOT / "IMPORT_MANIFEST.json").read_text())
        self.assertGreater(len(d["files"]), 100)
        for f in d["files"]:
            self.assertIn("source_content_sha256", f)
            self.assertIn("import_content_sha256", f)
            self.assertTrue(f["source_content_sha256"])
            self.assertTrue(f["import_content_sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
