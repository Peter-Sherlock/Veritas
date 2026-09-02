"""Deterministic statement similarity for candidate aggregation (M2-1, D-040).

Claim identity downstream of the extraction pipeline is currently the
derived canonical key, so a real model's rewording of the same fact lands
as a new claim and the watching conclusions churn (C2, D-039). The
clusterer provides the deterministic half of the merge decision:

* **Hard guards** — two statements that disagree on any number/version
  token (``3.7`` vs ``3.8``) or on negation can never be the same
  assertion; the similarity function returns ``None`` (guarded apart).
* **Content-token Jaccard** — outside the guards, similarity is plain
  Jaccard over lowercased content tokens with a frozen stopword list.
  Numbers are excluded from the content set because they are guarded
  exactly; negation words are content tokens because they change meaning.

The judgment is deliberately conservative: a missed paraphrase leaves two
claims (the pre-M2 status quo), while a false merge corrupts a claim's
evidence set. The threshold is frozen from the M2-1 calibration against
the live DeepSeek recording and the gold assertions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


AGGREGATION_RULE_VERSION = "claim-clusters-1"

# Version-like numbers stay whole: "3.8" is one token, not ["3", "8"].
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")

_NEGATIONS = frozenset(
    {"not", "no", "never", "cannot", "nor", "neither", "without", "isn", "doesn", "don"}
)

# Words that carry no discriminative weight for technical paraphrases.
# Negation words are deliberately NOT stopwords — they are guarded instead.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "to", "of", "in", "on", "for", "and", "or", "with", "by", "as", "at",
        "it", "its", "that", "this", "these", "those", "when", "if", "then",
        "than", "from", "into", "can", "could", "should", "would", "will",
        "may", "might", "must", "do", "does", "did", "done", "you", "your",
        "we", "they", "them", "their", "there", "here", "also", "only",
        "just", "more", "most", "less", "least", "any", "all", "both",
        "each", "other", "some", "such", "very", "use", "used", "uses",
        "using", "how", "what", "which", "who", "why", "where", "while",
    }
)


def statement_tokens(statement: str) -> tuple[str, ...]:
    """Lowercase alphanumeric tokens, keeping dotted version numbers whole."""
    return tuple(_TOKEN_RE.findall(statement.lower()))


def number_tokens(tokens: tuple[str, ...]) -> frozenset[str]:
    """Tokens that contain a digit (versions, quantities, ports)."""
    return frozenset(token for token in tokens if any(ch.isdigit() for ch in token))


def negation_tokens(tokens: tuple[str, ...]) -> frozenset[str]:
    return frozenset(token for token in tokens if token in _NEGATIONS)


def content_tokens(statement: str) -> frozenset[str]:
    """Discriminative tokens: not stopwords, not numbers (guarded separately)."""
    tokens = statement_tokens(statement)
    return frozenset(
        token
        for token in tokens
        if token not in _STOPWORDS and not any(ch.isdigit() for ch in token)
    )


@dataclass(frozen=True)
class ClusterPolicy:
    """Frozen aggregation policy.

    ``min_jaccard=0.375`` was calibrated on the frozen M1-2C2 live
    recording (DeepSeek V4-Flash) against the 32 gold assertions: every
    manually-reviewed true paraphrase pair scores >= 0.385, every
    different-fact pair scores <= 0.364, and the number/negation guards
    remove the pairs no score should join. Cluster-level coverage of the
    gold assertions rises from 3/32 (exact keys) to 19/32 with zero
    observed false merges.
    """

    min_jaccard: float = 0.375
    rule_version: str = AGGREGATION_RULE_VERSION


def similarity(left: str, right: str) -> float | None:
    """Similarity between two statements.

    Returns ``None`` when the statements are guarded apart — disagreeing
    number/version tokens or disagreeing negation — because no similarity
    score can make them the same assertion. Otherwise returns the Jaccard
    overlap of their content tokens.
    """
    if not left.strip() or not right.strip():
        return None
    left_tokens = statement_tokens(left)
    right_tokens = statement_tokens(right)
    if number_tokens(left_tokens) != number_tokens(right_tokens):
        return None
    if negation_tokens(left_tokens) != negation_tokens(right_tokens):
        return None
    left_content = content_tokens(left)
    right_content = content_tokens(right)
    union = left_content | right_content
    if not union:
        # Both statements are pure numbers/negations; the guards above
        # already ensured they agree, so treat them as identical.
        return 1.0
    return len(left_content & right_content) / len(union)
