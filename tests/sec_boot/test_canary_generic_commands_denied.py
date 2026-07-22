import pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parents[2]
oc = (ROOT / "src/joao_orchestrator/providers/opencode_provider.py").read_text()
DANG = ["find *","grep *","sed *","cat *","head *","tail *","wc *","git diff*","git show*","python -m pytest*","python3 -m pytest*","pytest*"]
still = [c for c in DANG if re.search(r'"%s":\s*"allow"' % re.escape(c), oc)]
if still or re.search(r'"edit":\s*"allow"', oc):
    print("FAIL still allowed:", still, "edit:", bool(re.search(r'"edit":\s*"allow"', oc))); sys.exit(1)
print("OK generic commands + edit denied"); sys.exit(0)
