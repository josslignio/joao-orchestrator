import pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from joao_orchestrator.bubble.write_tier_policy import WriteTierDisabled
try:
    from joao_orchestrator.bubble.runtime import GLMReviewer
    GLMReviewer(); print("OK reviewer constructs (no over-gating)"); sys.exit(0)
except WriteTierDisabled:
    print("FAIL reviewer over-gated"); sys.exit(1)
except Exception as e:
    print("OK reviewer not write-gated:", type(e).__name__); sys.exit(0)
