"""Graph bridge: research output in, real corpus history events in, evolution out.

The bridge owns three translations (D-037):

1. corpus documents -> ``SourceVersion`` rows whose ids match the pipeline's
   ``source_version_id`` scheme (``<corpus_id>:<doc_id>@<version>``), so
   extraction evidence links to registered sources without rewriting;
2. a session bundle -> claims, evidence spans and edges in the evolution
   repository, plus initial assessments and conclusions so the P0 rule
   engine has a complete T0 state;
3. the corpus manifest history -> deterministic ``revise`` ChangeEvents
   between two real versions of a document, with the new source version
   already wired to supersede the old one.

Revision events carry empty ``changed_locators``: without a semantic diff
the whole source version is the change scope, so every evidence span
grounded in it re-enters verification.
"""

from __future__ import annotations

from typing import Any

from veritas.domain.enums import Assessment, ChangeType, ConclusionOutcome, EdgeType
from veritas.domain.models import (
    ChangeEvent,
    ClaimAssessment,
    ConclusionVersion,
    DependencyEdge,
    SourceVersion,
)
from veritas.evidence.rules import evaluate_claim, evaluate_conclusion
from veritas.extraction.models import ExtractionCandidateBundle
from veritas.search.local_corpus import LocalCorpusProvider
from veritas.storage.sqlite import SQLiteRepository


class GraphBridgeError(ValueError):
    """A stable, classifiable failure at the research/evolution boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class GraphBridge:
    """Load research graphs into the evolution repository (D-037)."""

    def __init__(
        self,
        repository: SQLiteRepository,
        corpus: LocalCorpusProvider,
    ) -> None:
        self._repository = repository
        self._corpus = corpus

    def _fetch(self, doc_id: str, version_id: str):
        try:
            return self._corpus.fetch(doc_id, version_id)
        except KeyError as exc:
            raise GraphBridgeError(
                "unknown_corpus_version",
                f"corpus {self._corpus.corpus_id!r} has no {doc_id!r}@{version_id!r} snapshot",
            ) from exc

    def source_version_id(self, doc_id: str, version_id: str) -> str:
        return f"{self._corpus.corpus_id}:{doc_id}@{version_id}"

    def register_source_version(
        self,
        doc_id: str,
        version_id: str,
        *,
        observed_at: str,
        supersedes_version_id: str | None = None,
    ) -> SourceVersion:
        document = self._fetch(doc_id, version_id)
        source = SourceVersion(
            source_id=f"{self._corpus.corpus_id}:{doc_id}",
            version_id=self.source_version_id(doc_id, version_id),
            version_label=version_id,
            canonical_uri=f"local-corpus://{self._corpus.corpus_id}/{doc_id}",
            content_hash=document.content_hash,
            published_at=document.published_at,
            observed_at=observed_at,
            valid_from=document.published_at or observed_at,
            supersedes_version_id=supersedes_version_id,
        )
        self._repository.insert_source_version(source)
        return source

    def load_bundle(
        self,
        bundle: ExtractionCandidateBundle,
        *,
        observed_at: str,
    ) -> dict[str, int]:
        """Register the bundle's sources and insert its candidate graph."""
        before = self._repository.entity_counts()
        for document in bundle.documents:
            self.register_source_version(
                document.doc_id, document.version_id, observed_at=observed_at
            )
        for evidence in bundle.evidence_spans:
            if not self._repository.source_version_exists(evidence.source_version_id):
                raise GraphBridgeError(
                    "unregistered_source_version",
                    f"evidence {evidence.evidence_id!r} references "
                    f"{evidence.source_version_id!r}, which the bundle did not register; "
                    "the pipeline source_namespace does not match the corpus",
                )
        for claim in bundle.claims:
            self._repository.insert_claim(claim)
        for evidence in bundle.evidence_spans:
            self._repository.insert_evidence_span(evidence)
        for edge in bundle.edges:
            self._repository.insert_dependency_edge(edge)
        after = self._repository.entity_counts()
        return {
            name: after[name] - before[name]
            for name in ("source_versions", "claims", "evidence_spans", "dependency_edges")
        }

    def record_initial_assessments(
        self, *, snapshot_id: str, rule_version: str, reasoned_at: str
    ) -> list[str]:
        """Assess every claim that has no current assessment yet (T0)."""
        created: list[str] = []
        for claim in self._repository.list_claims():
            if self._repository.get_current_assessment(claim.claim_id) is not None:
                continue
            result = evaluate_claim(self._repository, claim.claim_id)
            assessment_id = self._repository.next_assessment_id(claim.claim_id)
            self._repository.insert_claim_assessment(
                ClaimAssessment(
                    assessment_id=assessment_id,
                    claim_id=claim.claim_id,
                    snapshot_id=snapshot_id,
                    assessment=result.assessment,
                    rule_version=rule_version,
                    reason_refs=result.reason_refs,
                    reasoned_at=reasoned_at,
                    supersedes_assessment_id=None,
                )
            )
            created.append(assessment_id)
        return created

    def record_initial_conclusion(
        self,
        *,
        conclusion_key: str,
        claim_ids: tuple[str, ...],
        pass_statement: str,
        fail_statement: str,
        rule_version: str,
        reasoned_at: str,
    ) -> ConclusionVersion:
        """Create conclusion v1 under an ``all_accepted`` rule with DEPENDS_ON edges."""
        if not claim_ids:
            raise GraphBridgeError("invalid_conclusion", "a conclusion needs at least one claim")
        dependency_rule: dict[str, Any] = {
            "kind": "all_accepted",
            "claim_ids": list(claim_ids),
            "pass_statement": pass_statement,
            "fail_statement": fail_statement,
        }
        draft = ConclusionVersion(
            conclusion_key=conclusion_key,
            conclusion_version_id=f"{conclusion_key}@1",
            version_number=1,
            statement=pass_statement,
            outcome=ConclusionOutcome.UNKNOWN,
            dependency_rule=dependency_rule,
            reason_refs=(),
            reasoned_at=reasoned_at,
            supersedes_conclusion_version_id=None,
        )
        result = evaluate_conclusion(self._repository, draft)
        conclusion = ConclusionVersion(
            conclusion_key=conclusion_key,
            conclusion_version_id=f"{conclusion_key}@1",
            version_number=1,
            statement=result.statement,
            outcome=result.outcome,
            dependency_rule=dependency_rule,
            reason_refs=result.reason_refs,
            reasoned_at=reasoned_at,
            supersedes_conclusion_version_id=None,
        )
        self._repository.insert_conclusion_version(conclusion)
        for claim_id in result.dependency_claim_ids:
            self._repository.insert_dependency_edge(
                DependencyEdge(
                    edge_id=f"EDGE_{claim_id}_TO_{conclusion.conclusion_version_id}",
                    edge_type=EdgeType.DEPENDS_ON,
                    from_id=claim_id,
                    to_id=conclusion.conclusion_version_id,
                    created_at=reasoned_at,
                    valid_from=reasoned_at,
                    valid_to=None,
                    rule_version=rule_version,
                )
            )
        return conclusion

    def revision_event(
        self,
        doc_id: str,
        old_version_id: str,
        new_version_id: str,
        *,
        project_id: str,
    ) -> tuple[ChangeEvent, SourceVersion]:
        """Build a revise ChangeEvent between two real corpus versions.

        The new version's ``published_at`` is both observed_at and
        effective_at — the event is grounded in when the upstream project
        actually published the revision, not in local processing time.
        """
        new_document = self._fetch(doc_id, new_version_id)
        old_version = self.source_version_id(doc_id, old_version_id)
        if not self._repository.source_version_exists(old_version):
            raise GraphBridgeError(
                "unregistered_old_source_version",
                f"{old_version!r} is not loaded into the "
                "evolution repository; run the T0 research pass first",
            )
        old_source = self._repository.get_source_version(old_version)
        new_source = self.register_source_version(
            doc_id,
            new_version_id,
            observed_at=new_document.published_at or "",
            supersedes_version_id=old_version,
        )
        if new_source.content_hash == old_source.content_hash:
            raise GraphBridgeError(
                "identical_content_revision",
                f"{doc_id!r} {old_version_id!r} and {new_version_id!r} share one content hash; "
                "no revision happened between them",
            )
        event = ChangeEvent(
            change_event_id=f"CHG_{doc_id.upper()}_{old_version_id}_TO_{new_version_id}",
            external_event_id=f"{self._corpus.corpus_id}/{doc_id}@{new_version_id}",
            project_id=project_id,
            change_type=ChangeType.REVISE,
            old_source_version_id=old_source.version_id,
            new_source_version_id=new_source.version_id,
            changed_locators=(),
            observed_at=new_document.published_at or "",
            effective_at=new_document.published_at or "",
        )
        return event, new_source
