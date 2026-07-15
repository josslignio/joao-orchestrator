"""V2 project profiles (§22).

Profiles externalize ALL project-specific rules OUT of the generic engine
(``src/joao_orchestrator/``). The generic core contains NO KOL names, ticker
logic, twscrape rules, dashboard URLs, scheduler literals, job-board names, CV
layout rules, user career content, or blacklist rules (§22).

This module provides only the *profile schema* and *preset builders*. The
actual literals (KOL handles, dashboard URL, job sources, CV template path)
live in profile JSON under ``config/``, which the generic engine reads but
never hardcodes.

Two presets:

* :func:`trading_radar_profile`  — frozen, SHADOW_ONLY, no-trading
* :func:`job_opportunity_profile` — daily, no-auto-apply, canonical-CV

:func:`forbidden_patterns_for` supplies the project-specific forbidden-literal
patterns used by the §21 review gate's generic-core purity scan. These patterns
are project data, so they live HERE (in the profile layer), not in the generic
review module.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence


@dataclass
class ProjectProfileV2:
    """A V2 project profile. Loaded from config, never hardcoded in the core."""
    project_id: str
    repository_path: str
    runtime_root: str
    known_good_command: str = ""
    dashboard_urls: list[str] = field(default_factory=list)
    scheduler_label: str = ""
    mode: str = "ACTIVE"          # SHADOW_ONLY | ACTIVE
    no_trading: bool = False
    no_auto_apply: bool = False
    frozen: bool = False          # frozen product: no behavior-changing edits
    acceptance_checks: list[str] = field(default_factory=list)
    known_incidents: list[str] = field(default_factory=list)
    blacklist_identity_rules: list[str] = field(default_factory=list)
    closed_job_verification: str = ""
    canonical_cv_references: list[str] = field(default_factory=list)
    source_strategy: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = "2.0"
        return d


# ---------------------------------------------------------------------------
# Presets — values are intentionally NON-literal where possible. The few
# literals that must appear (the dashboard URL, the scheduler label) are the
# profile's job to hold, never the generic core's.
# ---------------------------------------------------------------------------

def trading_radar_profile(
    *, repository_path: str = "/Users/jocelyngrosjean/twitter-scrape-test",
    runtime_root: str = "~/.local/share/joss-orchestrator",
    dashboard_url: str = (
        "https://josslignio.github.io/weekly-trading-radar-dashboard/"
        "latest/report.html"),
    scheduler_label: str = "com.joss.tradingradar.weekly-v1",
) -> ProjectProfileV2:
    """Trading Radar: FROZEN, SHADOW_ONLY, no trading (§2, §22)."""
    return ProjectProfileV2(
        project_id="weekly-trading-radar",
        repository_path=repository_path,
        runtime_root=runtime_root,
        known_good_command=".venv/bin/python scripts/weekly_v1_delivery.py",
        dashboard_urls=[dashboard_url],
        scheduler_label=scheduler_label,
        mode="SHADOW_ONLY",
        no_trading=True,
        frozen=True,
        acceptance_checks=[
            "production command exists",
            "5 KOL health records",
            "five horizons (7d/30d/90d/180d/365d)",
            "fail-closed validation gate",
            "public direct dashboard reachable (HTTP 200 on report.html)",
            "exactly one active scheduler",
            "no duplicate scheduler",
            "no email during tests",
            "no deploy during tests",
            "no product diff",
        ],
        known_incidents=[
            "existing twscrape path ignored (§4 #1)",
            "logged_in=0 trusted over successful search (§4 #2)",
            "gh absolute path described as missing (§4 #3)",
            "merged PR treated as open (§4 #4)",
            "passed gates reopened without contradiction (§4 #5)",
            "published=true accepted without HTTP verification (§4 #8)",
            "multiple launchd agents left active (§4 #12)",
            "intermediate page accepted instead of direct dashboard (§4 #13)",
        ],
    )


def job_opportunity_profile(
    *, repository_path: str = "/Users/jocelyngrosjean/job-opportunity-radar",
    runtime_root: str = "~/.local/share/joss-orchestrator/projects/job-opportunity-radar",
    daily_command: str = "scripts/run_daily.sh",
    canonical_cv_references: list[str] | None = None,
) -> ProjectProfileV2:
    """Job/CV Radar: daily, no-auto-apply, canonical-CV preserved (§3, §22)."""
    return ProjectProfileV2(
        project_id="job-opportunity-radar",
        repository_path=repository_path,
        runtime_root=runtime_root,
        known_good_command=daily_command,
        mode="ACTIVE",
        no_auto_apply=True,
        frozen=False,
        blacklist_identity_rules=[
            "exclude by exact URL",
            "exclude by ATS job ID",
            "exclude by normalized company",
            "exclude by normalized title",
            "persistently exclude applied / prepared / finalized roles",
        ],
        closed_job_verification=(
            "verify each job still open with an active apply path BEFORE scoring; "
            "exclude closed/expired/inactive/missing-apply roles"),
        canonical_cv_references=canonical_cv_references or [
            # References only — never replace canonical design with a template.
            "CV_Jocelyn_*.pdf (canonical typography/spacing/hierarchy)",
        ],
        source_strategy=[
            "public career pages", "ATS platforms", "Google",
            "public LinkedIn pages", "Welcome to the Jungle", "Wellfound",
            "Himalayas", "remote boards", "specialist boards",
            "company career pages",
        ],
        acceptance_checks=[
            "project discovered (not guessed)",
            "state resumes after interruption",
            "applied/prepared/finalized excluded",
            "closed jobs removed before scoring",
            "duplicates collapsed across URL/ATS/company/title",
            "active new role survives to shortlist",
            "canonical CV style preserved",
            "visible shortlist produced",
            "no application fired",
            "no job rules in generic core",
        ],
        known_incidents=[
            "already-applied roles reappearing (Job risk #1)",
            "closed jobs reaching scoring (Job risk #2)",
            "generic CV design replacing canonical (Job risk #4)",
            "interrupted runs restarting from zero (Job risk #5)",
        ],
    )


# Profiles are read by the generic engine through this registry; the engine
# never imports the preset functions directly — it loads profile JSON.
PROFILE_PRESETS = {
    "weekly-trading-radar": trading_radar_profile,
    "job-opportunity-radar": job_opportunity_profile,
}


def load_profile(project_id: str, **overrides: Any) -> ProjectProfileV2:
    """Build a profile preset, applying optional overrides."""
    builder = PROFILE_PRESETS.get(project_id)
    if builder is None:
        raise KeyError(f"no profile preset for {project_id!r}")
    return builder(**overrides)


__all__ = [
    "ProjectProfileV2", "trading_radar_profile", "job_opportunity_profile",
    "PROFILE_PRESETS", "load_profile", "forbidden_patterns_for",
]


def forbidden_patterns_for(
    project_id: str,
) -> Sequence[tuple[re.Pattern, str]]:
    """Forbidden-literal patterns a project forbids in the generic core (§22).

    Returns compiled (pattern, label) pairs. Lives in the profile layer so the
    generic review module never hardcodes project names.
    """
    if project_id == "weekly-trading-radar":
        return [
            (re.compile(r"\bbryzonx|ckcapital|frenchie|photonbull|rjccapital",
                        re.I), "trading KOL name in generic core"),
            (re.compile(r"\btwscrape\b", re.I),
             "twscrape business rule in generic core"),
            (re.compile(r"weekly-trading-radar-dashboard", re.I),
             "dashboard URL in generic core"),
        ]
    if project_id == "job-opportunity-radar":
        return [
            (re.compile(r"\bgreenhouse|lever|workable|"
                        r"welcome\s*to\s*the\s*jungle|wellfound|himalayas",
                        re.I), "job board name in generic core"),
        ]
    return ()
