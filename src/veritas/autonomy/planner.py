"""Autonomous re-research planning (M3-1).

The evolution engine tells the system *that* a conclusion needs attention
(outcome leaves PASS, `action_required` diffs). The planner decides *what
to research about it*: every non-PASS conclusion's dependency claims
become deterministic research items in the runtime session-spec format,
so the existing runtime CLI can execute the plan unchanged — and the
refresh applier (M3-2) can load the results back into the graph.

Planning rules (deterministic, auditable):

* only ``all_accepted`` conclusions are plannable; anything else is a
  ``PlanningError`` rather than a silently skipped conclusion;
* claims already watched by a planned item are not planned twice (the
  aggregate and per-claim conclusions may share claims);
* the item's question is the claim statement itself, and the query is
  its content tokens — the same deterministic vocabulary the aggregator
  uses, so retrieval, extraction and clustering all speak one language.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from veritas.aggregation.clusterer import content_tokens, number_tokens, statement_tokens
from veritas.domain.enums import ConclusionOutcome
from veritas.storage.sqlite import SQLiteRepository


class PlanningError(ValueError):
    """A stable, classifiable failure of the re-research planner."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class PlannedItem:
    item_id: str
    query: str
    question: str
    top_k: int

    def to_spec(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "query": self.query,
            "question": self.question,
            "top_k": self.top_k,
        }


@dataclass(frozen=True)
class ReSearchPlan:
    session_id: str
    budget_requests: int
    items: tuple[PlannedItem, ...]

    def to_spec(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "budget_requests": self.budget_requests,
            "items": [item.to_spec() for item in self.items],
        }

    def save(self, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(self.to_spec(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return output


def query_from_statement(statement: str) -> str:
    """Deterministic retrieval query: content and number tokens in
    statement order, deduplicated. Numbers are query terms — a version
    number is often the most discriminative word a claim has."""
    tokens = statement_tokens(statement)
    keep = content_tokens(statement) | number_tokens(tokens)
    seen: set[str] = set()
    terms: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        if token in keep:
            seen.add(token)
            terms.append(token)
    return " ".join(terms)


def plan_re_research(
    repository: SQLiteRepository,
    *,
    session_id: str,
    top_k: int = 3,
    requests_per_item: int = 3,
) -> ReSearchPlan:
    """Build a re-research plan for every conclusion not currently PASS."""
    if top_k < 1:
        raise PlanningError("invalid_top_k", "top_k must be >= 1")
    if requests_per_item < 1:
        raise PlanningError("invalid_budget", "requests_per_item must be >= 1")

    claims_by_id = {claim.claim_id: claim for claim in repository.list_claims()}
    planned_claims: set[str] = set()
    items: list[PlannedItem] = []
    conclusions = sorted(
        repository.list_current_conclusions(),
        key=lambda conclusion: conclusion.conclusion_key,
    )
    for index, conclusion in enumerate(conclusions, start=1):
        if conclusion.outcome == ConclusionOutcome.PASS:
            continue
        rule = conclusion.dependency_rule
        if rule.get("kind") != "all_accepted":
            raise PlanningError(
                "unsupported_rule_kind",
                f"conclusion {conclusion.conclusion_key!r} uses rule kind "
                f"{rule.get('kind')!r}; only all_accepted conclusions are plannable",
            )
        claim_ids = rule.get("claim_ids")
        if not claim_ids:
            raise PlanningError(
                "invalid_conclusion_rule",
                f"conclusion {conclusion.conclusion_key!r} has no dependency claims",
            )
        for claim_id in sorted(claim_ids):
            if claim_id in planned_claims:
                continue
            claim = claims_by_id.get(claim_id)
            if claim is None:
                raise PlanningError(
                    "unknown_claim",
                    f"conclusion {conclusion.conclusion_key!r} watches unknown "
                    f"claim {claim_id!r}",
                )
            planned_claims.add(claim_id)
            items.append(
                PlannedItem(
                    item_id=f"RR-{len(items) + 1:03d}",
                    query=query_from_statement(claim.statement),
                    question=claim.statement,
                    top_k=top_k,
                )
            )

    budget = max(1, requests_per_item * len(items))
    return ReSearchPlan(
        session_id=session_id, budget_requests=budget, items=tuple(items)
    )
