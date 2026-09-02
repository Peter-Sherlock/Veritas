"""M3-B scenario: one watch command runs the whole autonomous loop (D-043).

Starting state: the T0 graph is loaded against corpus 1.0 while the
corpus already carries 2.0. One ``run_watch_loop`` pass must:

1. detect the real content drift and apply the revision (no claims —
   re-research pending), flipping the watched claim to unsupported and
   the conclusion to unknown@2;
2. plan the re-research from the non-PASS conclusion;
3. run a real budgeted runtime session (replay provider) whose
   paraphrased result resolves back to the founder claim;
4. refresh the graph — claim ACCEPTED again, conclusion pass@3.

A second pass must be a clean no-op: no drift, empty plan, no requests,
no refreshes.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from veritas.aggregation import ClaimClusterStore
from veritas.aggregation.resolve import resolve_bundle
from veritas.autonomy import (
    apply_research_refresh,
    detect_drift,
    plan_re_research,
    run_t0_init,
)
from veritas.autonomy.cli import main as cli_main
from veritas.autonomy.watch import WatchLoopError, run_watch_loop
from veritas.domain.enums import ConclusionOutcome
from veritas.extraction.pipeline import (
    EXTRACTION_SYSTEM_PROMPT,
    ResearchExtractionPipeline,
    build_extraction_prompt,
    claim_id_for,
    derive_canonical_key,
)
from veritas.integration import GraphBridge
from veritas.invalidation.repair import ChangePackage, EvolutionEngine
from veritas.providers.llm import FixtureLLM, fixture_key
from veritas.runtime import ResearchRuntime, RuntimeStore, WorkItem
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.storage.sqlite import SQLiteRepository


QUESTION = "Does HTTPX retry failed connection setups?"
QUERY = "retries connection setup failures"
TOP_K = 3
RULE_VERSION = "p0-rules-2"
CONCLUSION_KEY = "retry_fact_supported"

V1_CONTENT = "HTTPX retries connection setup failures by default.\n"
V2_CONTENT = (
    "HTTPX retries connection setup failures by default.\n"
    "\n"
    "See the retry policy documentation for the full behavior.\n"
)
QUOTE = "HTTPX retries connection setup failures"
FOUNDER_STATEMENT = "HTTPX retries connection setup failures."
PARAPHRASE_STATEMENT = "HTTPX automatically retries connection setup failures."
PLANNER_QUERY = "httpx retries connection setup failures"

T0_AS_OF = "2026-01-15T00:00:00Z"
OBSERVED_AT = "2026-08-30T02:00:00Z"


def _build_corpus(root: Path) -> LocalCorpusProvider:
    (root / "retries").mkdir(parents=True)
    versions = []
    for version_id, content, published_at in (
        ("1.0", V1_CONTENT, "2026-01-01T00:00:00Z"),
        ("2.0", V2_CONTENT, "2026-02-01T00:00:00Z"),
    ):
        (root / "retries" / f"{version_id}.md").write_text(content, encoding="utf-8", newline="\n")
        versions.append(
            {
                "version_id": version_id,
                "path": f"retries/{version_id}.md",
                "published_at": published_at,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "source_ref": f"fixture:{version_id}",
            }
        )
    manifest = {
        "corpus_id": "m3b-corpus",
        "documents": [
            {"doc_id": "retries", "title": "retries", "versions": versions}
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return LocalCorpusProvider(root)


def _session_recording(corpus: LocalCorpusProvider, root: Path) -> Path:
    """Replay recording for the runtime session: one paraphrased assertion.

    The session's question is the planner's item question — the claim
    statement itself — so the prompts must be keyed with exactly that.
    """
    responses: dict[str, str] = {}
    for result in corpus.search(PLANNER_QUERY, top_k=TOP_K, as_of=None):
        document = corpus.fetch(result.doc_id, result.version_id)
        payload = {
            "assertions": [
                {"statement": PARAPHRASE_STATEMENT, "relation": "supports", "quote": QUOTE}
            ]
        }
        responses[
            fixture_key(
                EXTRACTION_SYSTEM_PROMPT,
                build_extraction_prompt(FOUNDER_STATEMENT, document),
            )
        ] = json.dumps(payload, ensure_ascii=False)
    path = root / "session-recording.json"
    path.write_text(
        json.dumps({"model_id": "m3b-model", "responses": responses}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _load_t0(corpus: LocalCorpusProvider, repository, clusters) -> str:
    t0_document = corpus.fetch("retries", "1.0")
    responses = {
        fixture_key(
            EXTRACTION_SYSTEM_PROMPT, build_extraction_prompt(QUESTION, t0_document)
        ): json.dumps(
            {
                "assertions": [
                    {
                        "statement": FOUNDER_STATEMENT,
                        "relation": "supports",
                        "quote": QUOTE,
                    }
                ]
            }
        )
    }
    pipeline = ResearchExtractionPipeline(
        corpus,
        FixtureLLM(responses, model_id="m3b-model"),
        source_namespace=corpus.corpus_id,
    )
    bundle = pipeline.run(
        query=QUERY, question=QUESTION, reasoned_at=OBSERVED_AT, top_k=TOP_K, as_of=T0_AS_OF
    )
    bundle = resolve_bundle(bundle, clusters, observed_at=OBSERVED_AT)
    bridge = GraphBridge(repository, corpus)
    bridge.load_bundle(bundle, observed_at=OBSERVED_AT)
    bridge.record_initial_assessments(
        snapshot_id="M3B:T0", rule_version=RULE_VERSION, reasoned_at=OBSERVED_AT
    )
    claim_id = next(claim.claim_id for claim in repository.list_claims())
    bridge.record_initial_conclusion(
        conclusion_key=CONCLUSION_KEY,
        claim_ids=(claim_id,),
        pass_statement="The retry fact is supported by current sources.",
        fail_statement="The retry fact is no longer supported; re-research required.",
        rule_version=RULE_VERSION,
        reasoned_at=OBSERVED_AT,
    )
    return claim_id


def _snapshot_hash(repository: SQLiteRepository) -> str:
    state = {
        "claims": [claim.to_dict() for claim in repository.list_claims()],
        "edges": [edge.to_dict() for edge in repository.list_dependency_edges()],
    }
    canonical = json.dumps(
        state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run_research_without_delivery(
    *,
    corpus: LocalCorpusProvider,
    repository: SQLiteRepository,
    clusters: ClaimClusterStore,
    runtime_store: RuntimeStore,
    recording: Path,
    session_id: str,
    crash_after_output: bool,
) -> None:
    drift = detect_drift(repository, corpus)[0]
    event, new_source = GraphBridge(repository, corpus).revision_event(
        drift.doc_id,
        drift.current_version,
        drift.latest_version,
        project_id="m3b-watch",
        observed_at=OBSERVED_AT,
    )
    EvolutionEngine(repository).apply(
        ChangePackage(
            scenario_id="watch",
            scenario_version="1.0.0",
            input_snapshot_id=f"watch:{session_id}",
            input_snapshot_hash=_snapshot_hash(repository),
            rule_version=RULE_VERSION,
            event=event,
            new_source=new_source,
            new_claims=(),
            new_evidence=(),
            new_edges=(),
        )
    )
    plan = plan_re_research(repository, session_id=session_id)
    runtime = ResearchRuntime(
        search=corpus,
        provider=FixtureLLM.from_json(recording),
        store=runtime_store,
        source_namespace=corpus.corpus_id,
        cluster_store=clusters,
    )

    def crash(_: object) -> None:
        raise RuntimeError("simulated_process_exit_after_output_checkpoint")

    if crash_after_output:
        with unittest.TestCase().assertRaisesRegex(
            RuntimeError, "simulated_process_exit"
        ):
            runtime.run(
                session_id=session_id,
                items=[WorkItem(**item.to_spec()) for item in plan.items],
                budget_requests=plan.budget_requests,
                observed_at=OBSERVED_AT,
                on_item_bundle=crash,
            )
    else:
        runtime.run(
            session_id=session_id,
            items=[WorkItem(**item.to_spec()) for item in plan.items],
            budget_requests=plan.budget_requests,
            observed_at=OBSERVED_AT,
        )


class WatchLoopM3BTests(unittest.TestCase):
    def test_restart_delivers_output_checkpointed_before_session_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = _build_corpus(root / "corpus")
            recording = _session_recording(corpus, root)
            repository = SQLiteRepository(root / "evolution.db")
            clusters = ClaimClusterStore(root / "clusters.sqlite3")
            runtime_store = RuntimeStore(root / "runtime.db")
            _load_t0(corpus, repository, clusters)
            _run_research_without_delivery(
                corpus=corpus,
                repository=repository,
                clusters=clusters,
                runtime_store=runtime_store,
                recording=recording,
                session_id="watch-crash-output",
                crash_after_output=True,
            )
            self.assertEqual(
                "active",
                runtime_store.session_state("watch-crash-output")["status"],
            )
            self.assertEqual(
                "pending",
                runtime_store.get_item_output(
                    "watch-crash-output", "RR-001"
                )["delivery_status"],
            )
            repository.close()
            clusters.close()
            runtime_store.close()

            repository = SQLiteRepository(root / "evolution.db")
            clusters = ClaimClusterStore(root / "clusters.sqlite3")
            runtime_store = RuntimeStore(root / "runtime.db")
            try:
                recovered = run_watch_loop(
                    repository=repository,
                    corpus=corpus,
                    provider=FixtureLLM.from_json(recording),
                    runtime_store=runtime_store,
                    cluster_store=clusters,
                    session_id="watch-crash-output",
                    observed_at=OBSERVED_AT,
                    project_id="m3b-watch",
                    rule_version=RULE_VERSION,
                )
                self.assertEqual("completed", recovered["session"]["status"])
                self.assertEqual(1, recovered["session"]["requests_spent"])
                self.assertEqual(
                    {"outputs": 1, "pending": 0, "applied": 1, "ignored": 0},
                    recovered["outbox"],
                )
                self.assertEqual(
                    ConclusionOutcome.PASS,
                    repository.get_current_conclusion(CONCLUSION_KEY).outcome,
                )
            finally:
                repository.close()
                clusters.close()
                runtime_store.close()

    def test_restart_replays_refresh_when_graph_commit_precedes_outbox_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = _build_corpus(root / "corpus")
            recording = _session_recording(corpus, root)
            repository = SQLiteRepository(root / "evolution.db")
            clusters = ClaimClusterStore(root / "clusters.sqlite3")
            runtime_store = RuntimeStore(root / "runtime.db")
            _load_t0(corpus, repository, clusters)
            _run_research_without_delivery(
                corpus=corpus,
                repository=repository,
                clusters=clusters,
                runtime_store=runtime_store,
                recording=recording,
                session_id="watch-crash-ack",
                crash_after_output=False,
            )
            output = runtime_store.get_item_output("watch-crash-ack", "RR-001")
            first_refresh = apply_research_refresh(
                repository,
                bundle=output["bundle"],
                session_id="watch-crash-ack",
                rule_version=RULE_VERSION,
                refreshed_at=OBSERVED_AT,
            )
            before_restart = repository.entity_counts()
            self.assertEqual("pending", output["delivery_status"])
            repository.close()
            clusters.close()
            runtime_store.close()

            repository = SQLiteRepository(root / "evolution.db")
            clusters = ClaimClusterStore(root / "clusters.sqlite3")
            runtime_store = RuntimeStore(root / "runtime.db")
            try:
                recovered = run_watch_loop(
                    repository=repository,
                    corpus=corpus,
                    provider=FixtureLLM.from_json(recording),
                    runtime_store=runtime_store,
                    cluster_store=clusters,
                    session_id="watch-crash-ack",
                    observed_at=OBSERVED_AT,
                    project_id="m3b-watch",
                    rule_version=RULE_VERSION,
                )
                self.assertEqual(before_restart, repository.entity_counts())
                self.assertEqual(
                    first_refresh["refresh_id"],
                    recovered["refreshes"][0]["refresh_id"],
                )
                self.assertEqual(
                    "applied",
                    runtime_store.get_item_output(
                        "watch-crash-ack", "RR-001"
                    )["delivery_status"],
                )
            finally:
                repository.close()
                clusters.close()
                runtime_store.close()

    def test_one_command_closes_the_loop_and_second_pass_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = _build_corpus(root / "corpus")
            repository = SQLiteRepository(root / "evolution.db")
            clusters = ClaimClusterStore(root / "clusters.sqlite3")
            runtime_store = RuntimeStore(root / "runtime.db")
            try:
                claim_id = _load_t0(corpus, repository, clusters)
                self.assertEqual(
                    ConclusionOutcome.PASS,
                    repository.get_current_conclusion(CONCLUSION_KEY).outcome,
                )
                # Before anything runs, the drift is visible.
                drifts = detect_drift(repository, corpus)
                self.assertEqual([("retries", "1.0", "2.0")], [
                    (d.doc_id, d.current_version, d.latest_version) for d in drifts
                ])

                recording = _session_recording(corpus, root)
                first = run_watch_loop(
                    repository=repository,
                    corpus=corpus,
                    provider=FixtureLLM.from_json(recording),
                    runtime_store=runtime_store,
                    cluster_store=clusters,
                    session_id="watch-1",
                    observed_at=OBSERVED_AT,
                    project_id="m3b-watch",
                    rule_version=RULE_VERSION,
                )
                self.assertEqual(
                    [
                        {
                            "change_event_id": "CHG_RETRIES_1.0_TO_2.0",
                            "doc_id": "retries",
                            "old_version": "1.0",
                            "new_version": "2.0",
                        }
                    ],
                    first["drift_applied"],
                )
                self.assertEqual(1, len(first["plan"]["items"]))
                self.assertEqual(FOUNDER_STATEMENT, first["plan"]["items"][0]["question"])
                self.assertEqual("completed", first["session"]["status"])
                self.assertEqual(1, first["session"]["requests_spent"])
                self.assertEqual(1, len(first["refreshes"]))
                self.assertIn(claim_id, first["refreshes"][0]["semantic_changed_claims"])
                self.assertEqual(
                    {"retry_fact_supported": "pass"}, first["final_conclusion_outcomes"]
                )
                repaired = repository.get_current_conclusion(CONCLUSION_KEY)
                self.assertEqual(3, repaired.version_number)
                self.assertEqual(ConclusionOutcome.PASS, repaired.outcome)

                # The same session may be reopened later, but its semantic
                # execution context stays pinned to the original run.
                resumed = run_watch_loop(
                    repository=repository,
                    corpus=corpus,
                    provider=FixtureLLM.from_json(recording),
                    runtime_store=runtime_store,
                    cluster_store=clusters,
                    session_id="watch-1",
                    observed_at="2026-09-01T00:00:00Z",
                    project_id="m3b-watch",
                    rule_version=RULE_VERSION,
                )
                self.assertEqual("completed", resumed["session"]["status"])
                self.assertEqual(1, resumed["outbox"]["applied"])
                with self.assertRaises(WatchLoopError) as caught:
                    run_watch_loop(
                        repository=repository,
                        corpus=corpus,
                        provider=FixtureLLM.from_json(recording),
                        runtime_store=runtime_store,
                        cluster_store=clusters,
                        session_id="watch-1",
                        observed_at=OBSERVED_AT,
                        project_id="m3b-watch",
                        rule_version="different-rules",
                    )
                self.assertEqual("session_context_drift", caught.exception.code)

                # Second pass: no drift, nothing to plan, nothing to spend.
                second = run_watch_loop(
                    repository=repository,
                    corpus=corpus,
                    provider=FixtureLLM.from_json(recording),
                    runtime_store=runtime_store,
                    cluster_store=clusters,
                    session_id="watch-2",
                    observed_at=OBSERVED_AT,
                    project_id="m3b-watch",
                    rule_version=RULE_VERSION,
                )
                self.assertEqual([], second["drift_applied"])
                self.assertEqual([], second["plan"]["items"])
                self.assertEqual(0, second["session"]["requests_spent"])
                self.assertEqual([], second["refreshes"])
                self.assertEqual(
                    {"retry_fact_supported": "pass"}, second["final_conclusion_outcomes"]
                )
            finally:
                clusters.close()
                runtime_store.close()
                repository.close()

    def test_cli_replay_runs_the_same_loop_from_one_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = _build_corpus(root / "corpus")
            repository = SQLiteRepository(root / "evolution.db")
            clusters = ClaimClusterStore(root / "clusters.sqlite3")
            runtime_store = RuntimeStore(root / "runtime.db")
            try:
                _load_t0(corpus, repository, clusters)
            finally:
                clusters.close()
                runtime_store.close()
                repository.close()

            recording = _session_recording(corpus, root)
            report_path = root / "watch-report.json"
            exit_code = cli_main(
                [
                    "--corpus-root", str(root / "corpus"),
                    "--evolution-store", str(root / "evolution.db"),
                    "--runtime-store", str(root / "runtime.db"),
                    "--cluster-store", str(root / "clusters.sqlite3"),
                    "--provider", "replay",
                    "--record-in", str(recording),
                    "--session-id", "watch-cli",
                    "--observed-at", OBSERVED_AT,
                    "--project-id", "m3b-watch",
                    "--output", str(report_path),
                ]
            )
            self.assertEqual(0, exit_code)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(report["drift_applied"]))
            self.assertEqual("completed", report["session"]["status"])
            self.assertEqual(
                {"retry_fact_supported": "pass"}, report["final_conclusion_outcomes"]
            )

    def test_t0_init_bootstraps_and_is_rerun_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corpus = _build_corpus(root / "corpus")
            repository = SQLiteRepository(root / "evolution.db")
            clusters = ClaimClusterStore(root / "clusters.sqlite3")
            runtime_store = RuntimeStore(root / "runtime.db")
            try:
                spec = {
                    "session_id": "m3b-init",
                    "items": [
                        {
                            "item_id": "EX-A",
                            "query": "retries",
                            "question": QUESTION,
                            "top_k": 3,
                        }
                    ],
                }
                t0_document = corpus.fetch("retries", "2.0")
                responses = {
                    fixture_key(
                        EXTRACTION_SYSTEM_PROMPT,
                        build_extraction_prompt(QUESTION, t0_document),
                    ): json.dumps(
                        {
                            "assertions": [
                                {
                                    "statement": PARAPHRASE_STATEMENT,
                                    "relation": "supports",
                                    "quote": QUOTE,
                                }
                            ]
                        }
                    )
                }
                provider = FixtureLLM(responses, model_id="m3b-model")
                init = run_t0_init(
                    repository=repository,
                    corpus=corpus,
                    provider=provider,
                    cluster_store=clusters,
                    spec=spec,
                    observed_at=OBSERVED_AT,
                    rule_version=RULE_VERSION,
                )
                self.assertEqual(1, init["assessments"])
                self.assertEqual(
                    [{"conclusion_key": "t0_ex_a", "outcome": "pass"}],
                    init["conclusions"],
                )
                claims = init["items"][0]["claims"]
                self.assertEqual(1, len(claims))
                # The bootstrap claim is the cluster representative's id.
                self.assertEqual(
                    claim_id_for(derive_canonical_key(PARAPHRASE_STATEMENT)),
                    claims[0],
                )
                # The bootstrap conclusion is the stored current view.
                from veritas.domain.enums import ConclusionOutcome

                self.assertEqual(
                    ConclusionOutcome.PASS,
                    repository.get_current_conclusion("t0_ex_a").outcome,
                )
                counts = repository.entity_counts()
                rerun = run_t0_init(
                    repository=repository,
                    corpus=corpus,
                    provider=provider,
                    cluster_store=clusters,
                    spec=spec,
                    observed_at=OBSERVED_AT,
                    rule_version=RULE_VERSION,
                )
                # Rerun-safe: nothing new lands. Already-assessed claims
                # are skipped (assessments: 0), conclusions unchanged.
                self.assertEqual(0, rerun["assessments"])
                self.assertEqual(init["conclusions"], rerun["conclusions"])
                self.assertEqual(counts, repository.entity_counts())
            finally:
                clusters.close()
                runtime_store.close()
                repository.close()


if __name__ == "__main__":
    unittest.main()
