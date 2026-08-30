"""Research runtime: session state, work queue, checkpoints and budget.

Session checkpoints live in their own mutable store while evidence stays
append-only (D-032/D-034); the request budget is enforced with
reserve-then-call accounting so a crash can underspend but never overspend.
"""

from veritas.runtime.engine import (
    BudgetExhausted,
    ReplanPolicy,
    ResearchRuntime,
    RuntimeSessionError,
    WorkItem,
)
from veritas.runtime.store import (
    SESSION_ACTIVE,
    SESSION_BUDGET_EXHAUSTED,
    SESSION_COMPLETED,
    RUNTIME_SCHEMA,
    RuntimeStore,
    RuntimeStoreError,
)

__all__ = [
    "BudgetExhausted",
    "RUNTIME_SCHEMA",
    "ReplanPolicy",
    "ResearchRuntime",
    "RuntimeSessionError",
    "RuntimeStore",
    "RuntimeStoreError",
    "WorkItem",
    "SESSION_ACTIVE",
    "SESSION_BUDGET_EXHAUSTED",
    "SESSION_COMPLETED",
]
