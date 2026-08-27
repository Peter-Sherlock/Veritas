from enum import StrEnum


class Assessment(StrEnum):
    ACCEPTED = "accepted"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    CONFLICT = "conflict"


class ConclusionOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class ChangeType(StrEnum):
    REVISE = "revise"
    RETRACT = "retract"
    EXPIRE = "expire"
    CONFLICT = "conflict"
    SUPERSEDE = "supersede"


class EdgeType(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    DEPENDS_ON = "depends_on"
    SUPERSEDES = "supersedes"

