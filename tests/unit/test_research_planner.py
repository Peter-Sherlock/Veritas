from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from veritas.autonomy import PlanningError, plan_re_research, query_from_statement
from veritas.domain.enums import Assessment, ConclusionOutcome, EdgeType
from veritas.domain.models import (
    Claim,
    ClaimAssessment,
    ConclusionVersion,
    DependencyEdge,
)
from veritas.storage.sqlite import SQLiteRepository


REASONED_AT = "2026-08-30T00:00:00Z"


def _claim(claim_id: str, statement: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        statement=statement,
        created_at=REASONED_AT,
        canonical_key=statement.lower().replace(" ", "_").rstrip("."),
    )


def _conclusion(key: str, claim_ids: tuple[str, ...], outcome: ConclusionOutcome) -> ConclusionVersion:
    return ConclusionVersion(
        conclusion_key=key,
        conclusion_version_id=f"{key}@1",
        version_number=1,
        statement="pass" if outcome == ConclusionOutcome.PASS else "fail",
        outcome=outcome,
        dependency_rule={
            "kind": "all_accepted",
            "claim_ids": list(claim_ids),
            "pass_statement": "pass",
            "fail_statement": "fail",
        },
        reason_refs=(),
        reasoned_at=REASONED_AT,
        supersedes_conclusion_version_id=None,
    )


class QueryFromStatementTests(unittest.TestCase):
    def test_stopwords_dropped_content_kept_in_order(self) -> None:
        self.assertEqual(
            "httpx requires python 3.7 later",
            query_from_statement("HTTPX requires Python 3.7 or later."),
        )

    def test_numbers_are_query_terms_and_repeats_deduplicated(self) -> None:
        self.assertEqual(
            "httpx supports http 2 needs alpn",
            query_from_statement("HTTPX supports HTTP/2. HTTP/2 needs ALPN."),
        )


class PlanReResearchTests(unittest.TestCase):
    def test_unknown_and_failed_conclusions_are_planned_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "plan.sqlite3")
            try:
                shared = _claim("claim:a", "HTTPX retries connection setup failures.")
                other = _claim("claim:b", "HTTPX normalizes line endings to LF.")
                for claim in (shared, other):
                    repository.insert_claim(claim)
                repository.insert_conclusion_version(
                    _conclusion("retry_fact", ("claim:a",), ConclusionOutcome.UNKNOWN)
                )
                repository.insert_conclusion_version(
                    _conclusion("line_endings", ("claim:b",), ConclusionOutcome.FAIL)
                )
                repository.insert_conclusion_version(
                    _conclusion("aggregate", ("claim:a", "claim:b"), ConclusionOutcome.UNKNOWN)
                )
                plan = plan_re_research(repository, session_id="m3-plan")
                # claim:a is watched by two unknown conclusions but is
                # planned once; the FAIL conclusion is planned too.
                self.assertEqual(
                    (
                        "HTTPX normalizes line endings to LF.",
                        "HTTPX retries connection setup failures.",
                    ),
                    tuple(sorted(item.question for item in plan.items)),
                )
                self.assertEqual(2, len(plan.items))
                self.assertEqual(6, plan.budget_requests)
                self.assertEqual("m3-plan", plan.session_id)
                self.assertEqual(
                    ["item_id", "query", "question", "top_k"],
                    sorted(plan.to_spec()["items"][0]),
                )
            finally:
                repository.close()

    def test_all_pass_conclusions_yield_an_empty_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "plan.sqlite3")
            try:
                repository.insert_claim(_claim("claim:a", "HTTPX retries failures."))
                repository.insert_conclusion_version(
                    _conclusion("retry_fact", ("claim:a",), ConclusionOutcome.PASS)
                )
                plan = plan_re_research(repository, session_id="m3-plan")
                self.assertEqual((), plan.items)
                self.assertEqual(1, plan.budget_requests)
            finally:
                repository.close()

    def test_non_all_accepted_rules_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "plan.sqlite3")
            try:
                conclusion = ConclusionVersion(
                    conclusion_key="threshold",
                    conclusion_version_id="threshold@1",
                    version_number=1,
                    statement="value",
                    outcome=ConclusionOutcome.UNKNOWN,
                    dependency_rule={"kind": "numeric_threshold", "minimum": 2},
                    reason_refs=(),
                    reasoned_at=REASONED_AT,
                    supersedes_conclusion_version_id=None,
                )
                repository.insert_conclusion_version(conclusion)
                with self.assertRaises(PlanningError) as caught:
                    plan_re_research(repository, session_id="m3-plan")
                self.assertEqual("unsupported_rule_kind", caught.exception.code)
            finally:
                repository.close()

    def test_conclusion_watching_unknown_claim_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = SQLiteRepository(Path(tmp) / "plan.sqlite3")
            try:
                repository.insert_conclusion_version(
                    _conclusion("dangling", ("claim:missing",), ConclusionOutcome.UNKNOWN)
                )
                with self.assertRaises(PlanningError) as caught:
                    plan_re_research(repository, session_id="m3-plan")
                self.assertEqual("unknown_claim", caught.exception.code)
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
