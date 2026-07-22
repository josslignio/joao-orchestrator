import pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from joao_orchestrator.bubble.write_tier_policy import WriteTierDisabled
import pathlib as P
fail = 0
def expect(desc, fn):
    global fail
    try:
        fn(); print("FAIL", desc, "did not raise"); fail = 1
    except WriteTierDisabled:
        print("OK", desc)
    except Exception as e:
        print("FAIL", desc, "->", type(e).__name__, "not WriteTierDisabled"); fail = 1
from joao_orchestrator.bubble.runtime import GLMBuilder, ClaudeCodeBuilder
expect("GLMBuilder.build", lambda: GLMBuilder().build("m", P.Path("/tmp"), P.Path("/tmp"), [], False))
expect("ClaudeCodeBuilder.build", lambda: ClaudeCodeBuilder().build("m", P.Path("/tmp"), P.Path("/tmp"), [], False))
from joao_orchestrator.providers.cascade_runtime import real_glm_runner, real_claude_runner
expect("cascade.real_glm_runner", lambda: real_glm_runner(P.Path("/tmp/x"))(P.Path("/tmp"), P.Path("/tmp/t"), P.Path("/tmp/o"), [], "a"))
expect("cascade.real_claude_runner", lambda: real_claude_runner()(P.Path("/tmp"), P.Path("/tmp/t"), P.Path("/tmp/o"), [], "a"))
from joao_orchestrator.runtime.convergence import _invoke_fixer
expect("convergence._invoke_fixer", lambda: _invoke_fixer(None, None, None, None, None))
# OpenCodeProvider.invoke: behavioral — bare instance (bypass __init__), gate must raise first
from joao_orchestrator.providers.opencode_provider import OpenCodeProvider
_inst = OpenCodeProvider.__new__(OpenCodeProvider)
expect("OpenCodeProvider.invoke", lambda: OpenCodeProvider.invoke(_inst, None))
from joao_orchestrator.providers.codex_subscription import CodexSubscriptionProvider
_cx = CodexSubscriptionProvider.__new__(CodexSubscriptionProvider)
expect("CodexSubscriptionProvider.invoke", lambda: CodexSubscriptionProvider.invoke(_cx, None))
sys.exit(fail)
