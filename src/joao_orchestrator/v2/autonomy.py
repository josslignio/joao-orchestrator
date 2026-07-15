"""V3 Autonomy Guard: product-first safe zero-click classification (C0.1 corrected).

This module is the corrective foundation. It encodes the hard rule the prior
over-claim violated:

    **Evidence flagged SIMULATED can never raise the autonomy level.**

C0.1 corrects the autonomy *model* (the C0 hard rule is preserved unchanged):

* accumulated verified capability evidence is monotone **for promotion**;
* the **active** autonomy level is **revocable** — a real circuit-breaker /
  no-progress / error-budget signal can demote L2 → L1 → L0;
* **REAL_REVIEWER_AVAILABLE alone cannot promote** to L1: promotion requires the
  *complete mechanical evidence set* for the level, not a single capability;
* promotion requires the measured **promotion window** defined by the
  Product-First contract (a named, observed window, not an instant claim);
* **no level may be skipped** (promotion is strictly stepwise L0→L1→L2→L3);
* L4 is never auto-derived (explicit product acceptance only).

Levels (V3 Product-First Safe Zero-Click contract):

    L0-SHADOW   observe + record only (CURRENT). The control plane, auto-merge
                and rollback plumbing are REAL, but the reviewer verdict and
                deploy lanes are SIMULATED.
    L1-ASSIST   real reviewer bridge proven + the L1 evidence set complete.
    L2-GUARDED  + real deploy target proven + the L2 evidence set complete.
    L3-AUTO     full zero-click within accepted scope; H2 still stops.
    L4-FULL     reserved; explicit product acceptance only (never auto).

Design invariants (unchanged): stdlib only; state outside the repo; generic core
has no project literals; fail-closed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Autonomy levels (string constants, per codebase convention — no Enum)
# ---------------------------------------------------------------------------

L0_SHADOW = "L0-SHADOW"
L1_ASSIST = "L1-ASSIST"
L2_GUARDED = "L2-GUARDED"
L3_AUTO = "L3-AUTO"
L4_FULL = "L4-FULL"

LEVELS = (L0_SHADOW, L1_ASSIST, L2_GUARDED, L3_AUTO, L4_FULL)
_LEVEL_RANK = {L0_SHADOW: 0, L1_ASSIST: 1, L2_GUARDED: 2, L3_AUTO: 3, L4_FULL: 4}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Evidence record
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    """One piece of evidence offered for an autonomy level.

    ``real=True`` evidence is verified (a tested real bridge/target). ``real=False``
    evidence is SIMULATED — recorded for honesty but mechanically unable to promote.
    A ``demoting=True`` piece of REAL evidence is a circuit-breaker signal that
    can revoke the active level (see AutonomyClassifier).
    """
    evidence_id: str
    capability: str          # e.g. "reviewer", "deploy_lanes", "circuit_breaker"
    claimed_level: str       # the level this evidence claims to support
    real: bool               # True = verified real; False = SIMULATED
    verified_by: str = ""    # how it was verified (test name / command)
    detail: str = ""
    demoting: bool = False   # True = a real circuit-breaker/no-progress signal

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def validate(self) -> None:
        if self.claimed_level not in _LEVEL_RANK:
            raise ValueError(f"claimed_level must be one of {LEVELS}")
        if not isinstance(self.real, bool):
            raise ValueError("evidence.real must be a bool")
        if not isinstance(self.demoting, bool):
            raise ValueError("evidence.demoting must be a bool")
        # A SIMULATED signal can never demote either (only real signals count).
        if self.demoting and not self.real:
            raise ValueError("a demoting signal must be real")


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------

@dataclass
class AutonomyClassification:
    """The derived autonomy level + the evidence that was considered."""
    level: str
    considered_evidence: list[dict[str, Any]] = field(default_factory=list)
    rejected_simulated: list[dict[str, Any]] = field(default_factory=list)
    demotion_signals: list[dict[str, Any]] = field(default_factory=list)
    promotion_window: str = ""
    rationale: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Promotion window
# ---------------------------------------------------------------------------

@dataclass
class PromotionWindow:
    """A measured promotion window (Product-First contract).

    A level is only promotable inside its named window. The window is a measured
    fact (e.g. "N consecutive green runs observed"), not an instant claim. An
    empty/unopened window blocks promotion even when all capabilities are real.
    """
    target_level: str
    opened: bool             # the window has been opened (measured)
    observation: str = ""    # what was measured

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# The classifier (C0.1 corrected semantics)
# ---------------------------------------------------------------------------

class AutonomyClassifier:
    """Classifies the autonomy level from a set of evidence + promotion window.

    Corrected model (C0.1):

    1. SIMULATED evidence never counts (the preserved C0 hard rule).
    2. Each level requires a *complete* real capability SET — not a single
       capability. In particular REAL_REVIEWER_AVAILABLE alone cannot promote to
       L1: L1 needs {reviewer, closure_matrix}.
    3. Promotion is strictly stepwise — no level may be skipped.
    4. Promotion requires the level's promotion window to be OPEN (measured).
    5. A REAL demoting signal (circuit_breaker / no_progress) REVOKES the active
       level downward (L2→L1→L0); the active level is revocable.
    """

    # The COMPLETE real capability SET required at each level (no single-cap
    # promotion). REAL_REVIEWER_AVAILABLE alone is insufficient for L1.
    LEVEL_REAL_GATES: dict[str, tuple[str, ...]] = {
        L1_ASSIST: ("reviewer", "closure_matrix"),
        L2_GUARDED: ("reviewer", "closure_matrix", "deploy_lanes"),
        L3_AUTO: ("reviewer", "closure_matrix", "deploy_lanes", "pipeline"),
    }

    def classify(self, evidence: list[Evidence],
                 windows: list[PromotionWindow] | None = None
                 ) -> AutonomyClassification:
        windows = windows or []
        considered: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        demotion: list[dict[str, Any]] = []
        real_capabilities: set[str] = set()

        for ev in evidence:
            ev.validate()
            if not ev.real:
                # SIMULATED evidence is recorded but CANNOT promote or demote.
                rejected.append(ev.to_dict())
                continue
            if ev.demoting:
                # A real circuit-breaker signal.
                demotion.append(ev.to_dict())
            else:
                considered.append(ev.to_dict())
                real_capabilities.add(ev.capability)

        open_windows = {w.target_level for w in windows if w.opened}

        # Stepwise promotion (no skipping): the highest level whose complete
        # real gate SET is satisfied AND whose window is open.
        level = L0_SHADOW
        for candidate in (L1_ASSIST, L2_GUARDED, L3_AUTO):
            gates = self.LEVEL_REAL_GATES[candidate]
            gates_satisfied = all(g in real_capabilities for g in gates)
            window_open = candidate in open_windows
            # Stepwise: can only reach candidate if we already reached the prior.
            prior_ok = (level == _prev_level(candidate)) or level == candidate
            if gates_satisfied and window_open and prior_ok:
                level = candidate
            else:
                break   # no skipping: stop at the first unsatisfied level
        # L4 is never auto-derived.

        # Active level is REVOCABLE: a real demotion signal pulls it down.
        level = self._apply_demotion(level, demotion)

        rationale = self._rationale(level, real_capabilities, rejected,
                                    demotion, open_windows)
        window_desc = "; ".join(
            f"{w.target_level}={'open' if w.opened else 'closed'}"
            for w in windows) or "none defined"
        return AutonomyClassification(
            level=level, considered_evidence=considered,
            rejected_simulated=rejected, demotion_signals=demotion,
            promotion_window=window_desc, rationale=rationale,
            timestamp=_utcnow())

    def _apply_demotion(self, level: str,
                        demotion: list[dict[str, Any]]) -> str:
        """A real circuit-breaker signal demotes the active level by one step
        per signal (L3→L2→L1→L0), never below L0."""
        rank = _LEVEL_RANK[level]
        for _ in demotion:
            rank = max(0, rank - 1)
        return _rank_to_level(rank)

    def _rationale(self, level: str, real_caps: set[str],
                   rejected: list[dict[str, Any]],
                   demotion: list[dict[str, Any]],
                   open_windows: set[str]) -> str:
        parts = [f"active level = {level} (revocable)"]
        parts.append(f"real capabilities = {sorted(real_caps) or 'none'}")
        parts.append(f"open promotion windows = {sorted(open_windows) or 'none'}")
        if rejected:
            parts.append(
                f"{len(rejected)} SIMULATED item(s) rejected (cannot promote): "
                f"{[r['capability'] for r in rejected]}")
        if demotion:
            parts.append(
                f"{len(demotion)} real demotion signal(s): "
                f"{[d['capability'] for d in demotion]}")
        return "; ".join(parts)


def _prev_level(level: str) -> str:
    """The level immediately below `level` (for the no-skip check)."""
    rank = _LEVEL_RANK[level]
    if rank == 0:
        return L0_SHADOW
    return _rank_to_level(rank - 1)


def _rank_to_level(rank: int) -> str:
    for name, r in _LEVEL_RANK.items():
        if r == rank:
            return name
    return L0_SHADOW


# ---------------------------------------------------------------------------
# The V3 current-state verdict (the honest baseline)
# ---------------------------------------------------------------------------

def current_state_evidence() -> list[Evidence]:
    """The honest evidence set for the system AS BUILT (PR-A/A2/A3).

    The plumbing is REAL, but the reviewer and deploy lanes are SIMULATED, and
    no closure_matrix / promotion window exists, so the derived level is L0-SHADOW.
    Under the C0.1 model, even a REAL reviewer alone would NOT promote (L1 needs
    {reviewer, closure_matrix} + an open window).
    """
    return [
        # REAL plumbing.
        Evidence("ev-genesis", "genesis", L1_ASSIST, real=True,
                 verified_by="scripts/test_joss_v2.py TestGenesis*",
                 detail="genesis gate is real and tested"),
        Evidence("ev-control-plane", "control_plane", L1_ASSIST, real=True,
                 verified_by="TestControlPlaneGates",
                 detail="classifier/store/queue/resume/isolation real + tested"),
        Evidence("ev-automerge", "automerge", L1_ASSIST, real=True,
                 verified_by="TestAutoMergeController; merged PR #25/#26/#27",
                 detail="gh pr merge via safe runner — actually merged 3 PRs"),
        Evidence("ev-rollback", "rollback", L1_ASSIST, real=True,
                 verified_by="TestRollbackController",
                 detail="git revert source rollback is real"),
        # SIMULATED — recorded honestly, CANNOT promote.
        Evidence("ev-reviewer", "reviewer", L1_ASSIST, real=False,
                 verified_by="DeterministicReviewerAdapter",
                 detail="SIMULATED reviewer (no live bridge tested)"),
        Evidence("ev-deploy", "deploy_lanes", L2_GUARDED, real=False,
                 verified_by="DeployController simulated=True",
                 detail="SIMULATED deploy lanes (no staging target locally)"),
    ]


def current_state_windows() -> list[PromotionWindow]:
    """No promotion window has been measured yet — all closed."""
    return [PromotionWindow(L1_ASSIST, opened=False,
                            observation="not measured"),
            PromotionWindow(L2_GUARDED, opened=False,
                            observation="not measured")]


def classify_current_state() -> AutonomyClassification:
    """The honest verdict: classify the system as built. Returns L0-SHADOW."""
    return AutonomyClassifier().classify(current_state_evidence(),
                                         current_state_windows())


__all__ = [
    "L0_SHADOW", "L1_ASSIST", "L2_GUARDED", "L3_AUTO", "L4_FULL", "LEVELS",
    "Evidence", "AutonomyClassification", "PromotionWindow", "AutonomyClassifier",
    "current_state_evidence", "current_state_windows", "classify_current_state",
]
