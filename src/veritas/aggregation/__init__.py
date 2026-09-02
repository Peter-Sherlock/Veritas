"""Candidate aggregation: deterministic claim identity clustering (M2-1)."""

from veritas.aggregation.clusterer import (
    AGGREGATION_RULE_VERSION,
    ClusterPolicy,
    similarity,
)
from veritas.aggregation.store import (
    CLUSTER_STORE_SCHEMA,
    ClaimClusterStore,
    ClaimClusterStoreError,
    Resolution,
)

__all__ = [
    "AGGREGATION_RULE_VERSION",
    "CLUSTER_STORE_SCHEMA",
    "ClaimClusterStore",
    "ClaimClusterStoreError",
    "ClusterPolicy",
    "Resolution",
    "similarity",
]
