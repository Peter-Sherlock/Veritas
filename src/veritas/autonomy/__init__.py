"""Autonomy: the agent decides what to research next (M3)."""

from veritas.autonomy.planner import (
    PlannedItem,
    PlanningError,
    ReSearchPlan,
    plan_re_research,
    query_from_statement,
)
from veritas.autonomy.refresh import RefreshError, apply_research_refresh
from veritas.autonomy.watch import (
    Drift,
    WatchLoopError,
    WebDrift,
    detect_drift,
    detect_web_drift,
    run_t0_init,
    run_watch_loop,
)

__all__ = [
    "Drift",
    "PlannedItem",
    "PlanningError",
    "ReSearchPlan",
    "RefreshError",
    "WatchLoopError",
    "WebDrift",
    "apply_research_refresh",
    "detect_drift",
    "detect_web_drift",
    "plan_re_research",
    "query_from_statement",
    "run_t0_init",
    "run_watch_loop",
]
