"""Bundle-level claim identity resolution (M2-1, D-040).

Rewrites an extraction bundle so that every claim's identity comes from
its cluster representative instead of its own canonical key: claims,
evidence edges and evidence-claim edge ids are remapped. Evidence spans
and the raw document/assertion records are untouched — the candidate
store keeps observing pre-aggregation truth (C2: expose, don't rewrite).

Claims that merge into one cluster collapse to a single claim row; their
evidence edges re-point to the surviving claim with recomputed edge ids.
"""

from __future__ import annotations

from veritas.aggregation.store import ClaimClusterStore
from veritas.domain.models import Claim, DependencyEdge
from veritas.extraction.models import ExtractionCandidateBundle
from veritas.extraction.pipeline import claim_id_for, edge_id_for


def resolve_bundle(
    bundle: ExtractionCandidateBundle,
    store: ClaimClusterStore,
    *,
    observed_at: str,
) -> ExtractionCandidateBundle:
    """Resolve every claim in the bundle through the cluster store."""
    id_map: dict[str, str] = {}
    ordered: list[tuple[str, str, str, str, Claim]] = []
    for claim in bundle.claims:
        resolution = store.resolve(
            canonical_key=claim.canonical_key,
            statement=claim.statement,
            observed_at=observed_at,
        )
        resolved_id = claim_id_for(resolution.representative_key)
        representative_statement = store.cluster_statement(
            resolution.representative_key
        )
        representative_created_at = store.cluster_created_at(
            resolution.representative_key
        )
        if representative_statement is None or representative_created_at is None:
            raise ValueError(
                "cluster representative disappeared during bundle resolution"
            )
        id_map[claim.claim_id] = resolved_id
        ordered.append(
            (
                resolved_id,
                resolution.representative_key,
                representative_statement,
                representative_created_at,
                claim,
            )
        )

    merged_claims: list[Claim] = []
    seen_claims: set[str] = set()
    for (
        resolved_id,
        representative_key,
        representative_statement,
        representative_created_at,
        claim,
    ) in ordered:
        if resolved_id in seen_claims:
            continue
        seen_claims.add(resolved_id)
        merged_claims.append(
            Claim(
                claim_id=resolved_id,
                statement=representative_statement,
                created_at=representative_created_at,
                canonical_key=representative_key,
            )
        )

    merged_edges: list[DependencyEdge] = []
    seen_edges: set[str] = set()
    for edge in bundle.edges:
        from_id = id_map.get(edge.from_id, edge.from_id)
        to_id = id_map.get(edge.to_id, edge.to_id)
        resolved_edge_id = edge_id_for(from_id, edge.edge_type.value, to_id)
        if resolved_edge_id in seen_edges:
            continue
        seen_edges.add(resolved_edge_id)
        merged_edges.append(
            DependencyEdge(
                edge_id=resolved_edge_id,
                edge_type=edge.edge_type,
                from_id=from_id,
                to_id=to_id,
                created_at=edge.created_at,
                valid_from=edge.valid_from,
                valid_to=edge.valid_to,
                rule_version=edge.rule_version,
            )
        )

    return ExtractionCandidateBundle(
        query=bundle.query,
        question=bundle.question,
        retrieved=bundle.retrieved,
        documents=bundle.documents,
        evidence_spans=bundle.evidence_spans,
        claims=tuple(merged_claims),
        edges=tuple(merged_edges),
    )
