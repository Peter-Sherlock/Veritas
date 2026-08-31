from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from veritas.domain.enums import Assessment, ChangeType, ConclusionOutcome, EdgeType
from veritas.domain.models import (
    ChangeEvent,
    Claim,
    ClaimAssessment,
    ConclusionVersion,
    DependencyEdge,
    EvidenceSpan,
    EvolutionRun,
    SourceVersion,
)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLiteRepository:
    """Append-oriented SQLite repository for the deterministic P0 runtime."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._initialize_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLiteRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_versions (
                version_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                version_label TEXT NOT NULL,
                canonical_uri TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                published_at TEXT,
                observed_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                supersedes_version_id TEXT REFERENCES source_versions(version_id),
                UNIQUE(source_id, content_hash)
            );

            CREATE TABLE IF NOT EXISTS evidence_spans (
                evidence_id TEXT PRIMARY KEY,
                source_version_id TEXT NOT NULL REFERENCES source_versions(version_id),
                locator_json TEXT NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                normalized_assertion TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT
            );

            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                statement TEXT NOT NULL,
                created_at TEXT NOT NULL,
                canonical_key TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS claim_assessments (
                assessment_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                snapshot_id TEXT NOT NULL,
                assessment TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                reason_refs_json TEXT NOT NULL,
                reasoned_at TEXT NOT NULL,
                supersedes_assessment_id TEXT REFERENCES claim_assessments(assessment_id)
            );

            CREATE TABLE IF NOT EXISTS conclusion_versions (
                conclusion_version_id TEXT PRIMARY KEY,
                conclusion_key TEXT NOT NULL,
                version_number INTEGER NOT NULL,
                statement TEXT NOT NULL,
                outcome TEXT NOT NULL,
                dependency_rule_json TEXT NOT NULL,
                reason_refs_json TEXT NOT NULL,
                reasoned_at TEXT NOT NULL,
                supersedes_conclusion_version_id TEXT REFERENCES conclusion_versions(conclusion_version_id),
                UNIQUE(conclusion_key, version_number)
            );

            CREATE TABLE IF NOT EXISTS dependency_edges (
                edge_id TEXT PRIMARY KEY,
                edge_type TEXT NOT NULL,
                from_id TEXT NOT NULL,
                to_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                rule_version TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS change_events (
                change_event_id TEXT PRIMARY KEY,
                external_event_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                change_type TEXT NOT NULL,
                old_source_version_id TEXT NOT NULL REFERENCES source_versions(version_id),
                new_source_version_id TEXT,
                changed_locators_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                effective_at TEXT NOT NULL,
                UNIQUE(project_id, external_event_id)
            );

            CREATE TABLE IF NOT EXISTS evolution_runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                external_event_id TEXT NOT NULL,
                change_event_id TEXT NOT NULL REFERENCES change_events(change_event_id),
                payload_json TEXT NOT NULL,
                UNIQUE(project_id, external_event_id)
            );

            CREATE TABLE IF NOT EXISTS scenario_snapshots (
                scenario_id TEXT NOT NULL,
                scenario_version TEXT NOT NULL,
                input_snapshot_id TEXT NOT NULL,
                input_snapshot_hash TEXT NOT NULL,
                loaded_at TEXT NOT NULL,
                PRIMARY KEY (scenario_id, scenario_version, input_snapshot_id)
            );

            CREATE TABLE IF NOT EXISTS research_refreshes (
                refresh_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                refreshed_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_edges_from ON dependency_edges(from_id, edge_type);
            CREATE INDEX IF NOT EXISTS idx_edges_to ON dependency_edges(to_id, edge_type);
            CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence_spans(source_version_id);
            CREATE INDEX IF NOT EXISTS idx_assessment_claim ON claim_assessments(claim_id);
            CREATE INDEX IF NOT EXISTS idx_conclusion_key ON conclusion_versions(conclusion_key);
            """
        )

    def _insert_immutable(
        self,
        *,
        table: str,
        id_column: str,
        id_value: str,
        columns: tuple[str, ...],
        values: tuple[Any, ...],
    ) -> None:
        """Insert an immutable row idempotently and reject payload conflicts."""
        column_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT OR IGNORE INTO {table} ({column_sql}) VALUES ({placeholders})",
            values,
        )
        row = self.connection.execute(
            f"SELECT {column_sql} FROM {table} WHERE {id_column} = ?",
            (id_value,),
        ).fetchone()
        if row is None or tuple(row[column] for column in columns) != values:
            raise ValueError(
                f"immutable_entity_conflict:{table}:{id_value}"
            )

    def insert_source_version(self, source: SourceVersion) -> None:
        self._insert_immutable(
            table="source_versions",
            id_column="version_id",
            id_value=source.version_id,
            columns=(
                "version_id",
                "source_id",
                "version_label",
                "canonical_uri",
                "content_hash",
                "published_at",
                "observed_at",
                "valid_from",
                "valid_to",
                "supersedes_version_id",
            ),
            values=(
                source.version_id,
                source.source_id,
                source.version_label,
                source.canonical_uri,
                source.content_hash,
                source.published_at,
                source.observed_at,
                source.valid_from,
                source.valid_to,
                source.supersedes_version_id,
            ),
        )

    def insert_evidence_span(self, evidence: EvidenceSpan) -> None:
        self._insert_immutable(
            table="evidence_spans",
            id_column="evidence_id",
            id_value=evidence.evidence_id,
            columns=(
                "evidence_id",
                "source_version_id",
                "locator_json",
                "text",
                "text_hash",
                "normalized_assertion",
                "valid_from",
                "valid_to",
            ),
            values=(
                evidence.evidence_id,
                evidence.source_version_id,
                _json_dump(evidence.locator),
                evidence.text,
                evidence.text_hash,
                evidence.normalized_assertion,
                evidence.valid_from,
                evidence.valid_to,
            ),
        )

    def insert_claim(self, claim: Claim) -> None:
        self._insert_immutable(
            table="claims",
            id_column="claim_id",
            id_value=claim.claim_id,
            columns=("claim_id", "statement", "created_at", "canonical_key"),
            values=(claim.claim_id, claim.statement, claim.created_at, claim.canonical_key),
        )

    def insert_claim_assessment(self, assessment: ClaimAssessment) -> None:
        self._insert_immutable(
            table="claim_assessments",
            id_column="assessment_id",
            id_value=assessment.assessment_id,
            columns=(
                "assessment_id",
                "claim_id",
                "snapshot_id",
                "assessment",
                "rule_version",
                "reason_refs_json",
                "reasoned_at",
                "supersedes_assessment_id",
            ),
            values=(
                assessment.assessment_id,
                assessment.claim_id,
                assessment.snapshot_id,
                assessment.assessment.value,
                assessment.rule_version,
                _json_dump(assessment.reason_refs),
                assessment.reasoned_at,
                assessment.supersedes_assessment_id,
            ),
        )

    def insert_conclusion_version(self, conclusion: ConclusionVersion) -> None:
        self._insert_immutable(
            table="conclusion_versions",
            id_column="conclusion_version_id",
            id_value=conclusion.conclusion_version_id,
            columns=(
                "conclusion_version_id",
                "conclusion_key",
                "version_number",
                "statement",
                "outcome",
                "dependency_rule_json",
                "reason_refs_json",
                "reasoned_at",
                "supersedes_conclusion_version_id",
            ),
            values=(
                conclusion.conclusion_version_id,
                conclusion.conclusion_key,
                conclusion.version_number,
                conclusion.statement,
                conclusion.outcome.value,
                _json_dump(conclusion.dependency_rule),
                _json_dump(conclusion.reason_refs),
                conclusion.reasoned_at,
                conclusion.supersedes_conclusion_version_id,
            ),
        )

    def insert_dependency_edge(self, edge: DependencyEdge) -> None:
        self._insert_immutable(
            table="dependency_edges",
            id_column="edge_id",
            id_value=edge.edge_id,
            columns=(
                "edge_id",
                "edge_type",
                "from_id",
                "to_id",
                "created_at",
                "valid_from",
                "valid_to",
                "rule_version",
            ),
            values=(
                edge.edge_id,
                edge.edge_type.value,
                edge.from_id,
                edge.to_id,
                edge.created_at,
                edge.valid_from,
                edge.valid_to,
                edge.rule_version,
            ),
        )

    def insert_change_event(self, event: ChangeEvent) -> None:
        self.connection.execute(
            """
            INSERT INTO change_events (
                change_event_id, external_event_id, project_id, change_type,
                old_source_version_id, new_source_version_id, changed_locators_json,
                observed_at, effective_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.change_event_id,
                event.external_event_id,
                event.project_id,
                event.change_type.value,
                event.old_source_version_id,
                event.new_source_version_id,
                _json_dump(event.changed_locators),
                event.observed_at,
                event.effective_at,
            ),
        )

    def insert_evolution_run(self, run: EvolutionRun) -> None:
        self.connection.execute(
            """
            INSERT INTO evolution_runs (
                run_id, project_id, external_event_id, change_event_id, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.project_id,
                run.external_event_id,
                run.change_event_id,
                _json_dump(run.to_dict()),
            ),
        )

    def find_evolution_run(self, project_id: str, external_event_id: str) -> EvolutionRun | None:
        row = self.connection.execute(
            "SELECT payload_json FROM evolution_runs WHERE project_id = ? AND external_event_id = ?",
            (project_id, external_event_id),
        ).fetchone()
        if row is None:
            return None
        return EvolutionRun.from_dict(json.loads(row["payload_json"]))

    def source_version_exists(self, version_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM source_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return row is not None

    def list_source_versions(self) -> list[SourceVersion]:
        rows = self.connection.execute(
            "SELECT * FROM source_versions ORDER BY version_id"
        ).fetchall()
        return [self._source_from_row(row) for row in rows]

    def source_is_active(self, version_id: str) -> bool:
        """The derived current-view test: a source is active while nothing
        supersedes it and no retract/expire event names it."""
        row = self.connection.execute(
            """
            SELECT 1
            WHERE EXISTS (
                SELECT 1 FROM source_versions AS newer
                WHERE newer.supersedes_version_id = ?
            )
            OR EXISTS (
                SELECT 1 FROM change_events AS event
                WHERE event.change_type IN ('retract', 'expire')
                  AND event.old_source_version_id = ?
            )
            """,
            (version_id, version_id),
        ).fetchone()
        return row is None

    def insert_research_refresh(
        self, refresh_id: str, session_id: str, refreshed_at: str, payload: dict[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO research_refreshes (refresh_id, session_id, refreshed_at, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (refresh_id, session_id, refreshed_at, _json_dump(payload)),
        )

    def find_research_refresh(self, refresh_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload_json FROM research_refreshes WHERE refresh_id = ?",
            (refresh_id,),
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def get_scenario_snapshot_hash(
        self,
        scenario_id: str,
        scenario_version: str,
        input_snapshot_id: str,
    ) -> str | None:
        row = self.connection.execute(
            """
            SELECT input_snapshot_hash
            FROM scenario_snapshots
            WHERE scenario_id = ? AND scenario_version = ? AND input_snapshot_id = ?
            """,
            (scenario_id, scenario_version, input_snapshot_id),
        ).fetchone()
        return None if row is None else str(row["input_snapshot_hash"])

    def register_scenario_snapshot(
        self,
        *,
        scenario_id: str,
        scenario_version: str,
        input_snapshot_id: str,
        input_snapshot_hash: str,
        loaded_at: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO scenario_snapshots (
                scenario_id, scenario_version, input_snapshot_id, input_snapshot_hash, loaded_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                scenario_id,
                scenario_version,
                input_snapshot_id,
                input_snapshot_hash,
                loaded_at,
            ),
        )

    def get_source_version(self, version_id: str) -> SourceVersion:
        row = self.connection.execute(
            "SELECT * FROM source_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown source version: {version_id}")
        return self._source_from_row(row)

    def list_evidence_for_source(self, source_version_id: str) -> list[EvidenceSpan]:
        rows = self.connection.execute(
            "SELECT * FROM evidence_spans WHERE source_version_id = ? ORDER BY evidence_id",
            (source_version_id,),
        ).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    def list_claims(self) -> list[Claim]:
        rows = self.connection.execute("SELECT * FROM claims ORDER BY claim_id").fetchall()
        return [self._claim_from_row(row) for row in rows]

    def get_claim(self, claim_id: str) -> Claim:
        row = self.connection.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown claim: {claim_id}")
        return self._claim_from_row(row)

    def list_dependency_edges(self) -> list[DependencyEdge]:
        rows = self.connection.execute("SELECT * FROM dependency_edges ORDER BY edge_id").fetchall()
        return [self._edge_from_row(row) for row in rows]

    def list_active_evidence_edges_for_claim(self, claim_id: str) -> list[DependencyEdge]:
        rows = self.connection.execute(
            """
            SELECT edge.*
            FROM dependency_edges AS edge
            JOIN evidence_spans AS evidence ON evidence.evidence_id = edge.from_id
            JOIN source_versions AS source ON source.version_id = evidence.source_version_id
            WHERE edge.to_id = ?
              AND edge.edge_type IN ('supports', 'contradicts')
              AND edge.valid_to IS NULL
              AND evidence.valid_to IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM source_versions AS newer
                  WHERE newer.supersedes_version_id = source.version_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM change_events AS event
                  WHERE event.change_type IN ('retract', 'expire')
                    AND event.old_source_version_id = source.version_id
              )
            ORDER BY edge.edge_id
            """,
            (claim_id,),
        ).fetchall()
        return [self._edge_from_row(row) for row in rows]

    def get_current_assessment(self, claim_id: str) -> ClaimAssessment | None:
        row = self.connection.execute(
            """
            SELECT assessment.*
            FROM claim_assessments AS assessment
            WHERE assessment.claim_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM claim_assessments AS newer
                  WHERE newer.supersedes_assessment_id = assessment.assessment_id
              )
            ORDER BY assessment.reasoned_at DESC, assessment.assessment_id DESC
            LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
        return None if row is None else self._assessment_from_row(row)

    def list_current_assessments(self) -> dict[str, ClaimAssessment]:
        return {
            claim.claim_id: assessment
            for claim in self.list_claims()
            if (assessment := self.get_current_assessment(claim.claim_id)) is not None
        }

    def next_assessment_id(self, claim_id: str) -> str:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM claim_assessments WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        return f"{claim_id}@assessment-{int(row['count']) + 1}"

    def get_conclusion(self, conclusion_version_id: str) -> ConclusionVersion:
        row = self.connection.execute(
            "SELECT * FROM conclusion_versions WHERE conclusion_version_id = ?",
            (conclusion_version_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown conclusion version: {conclusion_version_id}")
        return self._conclusion_from_row(row)

    def get_current_conclusion(self, conclusion_key: str) -> ConclusionVersion:
        row = self.connection.execute(
            """
            SELECT conclusion.*
            FROM conclusion_versions AS conclusion
            WHERE conclusion.conclusion_key = ?
              AND NOT EXISTS (
                  SELECT 1 FROM conclusion_versions AS newer
                  WHERE newer.supersedes_conclusion_version_id = conclusion.conclusion_version_id
              )
            ORDER BY conclusion.version_number DESC
            LIMIT 1
            """,
            (conclusion_key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown current conclusion: {conclusion_key}")
        return self._conclusion_from_row(row)

    def list_current_conclusions(self) -> list[ConclusionVersion]:
        rows = self.connection.execute(
            """
            SELECT conclusion.*
            FROM conclusion_versions AS conclusion
            WHERE NOT EXISTS (
                SELECT 1 FROM conclusion_versions AS newer
                WHERE newer.supersedes_conclusion_version_id = conclusion.conclusion_version_id
            )
            ORDER BY conclusion.conclusion_key
            """
        ).fetchall()
        return [self._conclusion_from_row(row) for row in rows]

    def next_conclusion_version(self, conclusion_key: str) -> tuple[int, str]:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) AS version FROM conclusion_versions WHERE conclusion_key = ?",
            (conclusion_key,),
        ).fetchone()
        version = int(row["version"]) + 1
        return version, f"{conclusion_key}@{version}"

    def entity_counts(self) -> dict[str, int]:
        tables = (
            "source_versions",
            "evidence_spans",
            "claims",
            "claim_assessments",
            "conclusion_versions",
            "dependency_edges",
            "change_events",
            "evolution_runs",
            "scenario_snapshots",
        )
        return {
            table: int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    def validate_provenance(self) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        for claim_id, assessment in self.list_current_assessments().items():
            active_edge_ids = {
                edge.edge_id for edge in self.list_active_evidence_edges_for_claim(claim_id)
            }
            for reason_ref in assessment.reason_refs:
                row = self.connection.execute(
                    "SELECT edge_type, to_id FROM dependency_edges WHERE edge_id = ?",
                    (reason_ref,),
                ).fetchone()
                if row is None:
                    errors.append(
                        {
                            "claim_id": claim_id,
                            "assessment_id": assessment.assessment_id,
                            "reason_ref": reason_ref,
                            "error": "missing_dependency_edge",
                        }
                    )
                    continue
                if row["to_id"] != claim_id:
                    errors.append(
                        {
                            "claim_id": claim_id,
                            "assessment_id": assessment.assessment_id,
                            "reason_ref": reason_ref,
                            "error": "edge_targets_different_claim",
                        }
                    )
                    continue
                if row["edge_type"] in ("supports", "contradicts") and reason_ref not in active_edge_ids:
                    errors.append(
                        {
                            "claim_id": claim_id,
                            "assessment_id": assessment.assessment_id,
                            "reason_ref": reason_ref,
                            "error": "inactive_evidence_reference",
                        }
                    )
        source_rows = self.connection.execute(
            """
            SELECT current.version_id AS entity_id,
                   current.source_id AS current_source_id,
                   current.supersedes_version_id AS predecessor_id,
                   predecessor.source_id AS predecessor_source_id
            FROM source_versions AS current
            LEFT JOIN source_versions AS predecessor
              ON predecessor.version_id = current.supersedes_version_id
            WHERE current.supersedes_version_id IS NOT NULL
            ORDER BY current.version_id
            """
        ).fetchall()
        for row in source_rows:
            if row["predecessor_source_id"] != row["current_source_id"]:
                errors.append(
                    {
                        "entity_id": row["entity_id"],
                        "entity_type": "source_version",
                        "predecessor_id": row["predecessor_id"],
                        "error": "source_supersedes_different_identity",
                    }
                )

        assessment_rows = self.connection.execute(
            """
            SELECT current.assessment_id AS entity_id,
                   current.claim_id AS current_claim_id,
                   current.supersedes_assessment_id AS predecessor_id,
                   predecessor.claim_id AS predecessor_claim_id
            FROM claim_assessments AS current
            LEFT JOIN claim_assessments AS predecessor
              ON predecessor.assessment_id = current.supersedes_assessment_id
            WHERE current.supersedes_assessment_id IS NOT NULL
            ORDER BY current.assessment_id
            """
        ).fetchall()
        for row in assessment_rows:
            if row["predecessor_claim_id"] != row["current_claim_id"]:
                errors.append(
                    {
                        "entity_id": row["entity_id"],
                        "entity_type": "claim_assessment",
                        "claim_id": row["current_claim_id"],
                        "predecessor_id": row["predecessor_id"],
                        "error": "assessment_supersedes_different_claim",
                    }
                )

        conclusion_rows = self.connection.execute(
            """
            SELECT current.conclusion_version_id AS entity_id,
                   current.conclusion_key AS current_key,
                   current.version_number AS current_version,
                   current.supersedes_conclusion_version_id AS predecessor_id,
                   predecessor.conclusion_key AS predecessor_key,
                   predecessor.version_number AS predecessor_version
            FROM conclusion_versions AS current
            LEFT JOIN conclusion_versions AS predecessor
              ON predecessor.conclusion_version_id = current.supersedes_conclusion_version_id
            WHERE current.supersedes_conclusion_version_id IS NOT NULL
            ORDER BY current.conclusion_version_id
            """
        ).fetchall()
        for row in conclusion_rows:
            if (
                row["predecessor_key"] != row["current_key"]
                or row["predecessor_version"] is None
                or int(row["current_version"]) != int(row["predecessor_version"]) + 1
            ):
                errors.append(
                    {
                        "entity_id": row["entity_id"],
                        "entity_type": "conclusion_version",
                        "predecessor_id": row["predecessor_id"],
                        "error": "broken_conclusion_supersedes_chain",
                    }
                )

        change_rows = self.connection.execute(
            """
            SELECT event.change_event_id AS entity_id,
                   event.change_type,
                   event.old_source_version_id,
                   event.new_source_version_id,
                   old_source.source_id AS old_source_id,
                   new_source.source_id AS new_source_id,
                   new_source.supersedes_version_id
            FROM change_events AS event
            LEFT JOIN source_versions AS old_source
              ON old_source.version_id = event.old_source_version_id
            LEFT JOIN source_versions AS new_source
              ON new_source.version_id = event.new_source_version_id
            ORDER BY event.change_event_id
            """
        ).fetchall()
        for row in change_rows:
            if row["change_type"] in (ChangeType.RETRACT.value, ChangeType.EXPIRE.value):
                valid = row["new_source_version_id"] is None
            elif row["change_type"] == ChangeType.CONFLICT.value:
                valid = (
                    row["new_source_version_id"] is not None
                    and row["supersedes_version_id"] is None
                    and row["new_source_id"] is not None
                    and row["new_source_id"] != row["old_source_id"]
                )
            else:
                valid = (
                    row["new_source_version_id"] is not None
                    and row["supersedes_version_id"] == row["old_source_version_id"]
                )
            if not valid:
                errors.append(
                    {
                        "entity_id": row["entity_id"],
                        "entity_type": "change_event",
                        "error": "change_event_source_lineage_mismatch",
                    }
                )
        return errors

    def validate_current_assessment_provenance(self) -> list[dict[str, Any]]:
        """Backward-compatible alias for the expanded P0-2 provenance audit."""
        return self.validate_provenance()

    @staticmethod
    def _source_from_row(row: sqlite3.Row) -> SourceVersion:
        return SourceVersion(
            source_id=row["source_id"],
            version_id=row["version_id"],
            version_label=row["version_label"],
            canonical_uri=row["canonical_uri"],
            content_hash=row["content_hash"],
            published_at=row["published_at"],
            observed_at=row["observed_at"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            supersedes_version_id=row["supersedes_version_id"],
        )

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> EvidenceSpan:
        return EvidenceSpan(
            evidence_id=row["evidence_id"],
            source_version_id=row["source_version_id"],
            locator=json.loads(row["locator_json"]),
            text=row["text"],
            text_hash=row["text_hash"],
            normalized_assertion=row["normalized_assertion"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
        )

    @staticmethod
    def _claim_from_row(row: sqlite3.Row) -> Claim:
        return Claim(
            claim_id=row["claim_id"],
            statement=row["statement"],
            created_at=row["created_at"],
            canonical_key=row["canonical_key"],
        )

    @staticmethod
    def _assessment_from_row(row: sqlite3.Row) -> ClaimAssessment:
        return ClaimAssessment(
            assessment_id=row["assessment_id"],
            claim_id=row["claim_id"],
            snapshot_id=row["snapshot_id"],
            assessment=Assessment(row["assessment"]),
            rule_version=row["rule_version"],
            reason_refs=tuple(json.loads(row["reason_refs_json"])),
            reasoned_at=row["reasoned_at"],
            supersedes_assessment_id=row["supersedes_assessment_id"],
        )

    @staticmethod
    def _conclusion_from_row(row: sqlite3.Row) -> ConclusionVersion:
        return ConclusionVersion(
            conclusion_key=row["conclusion_key"],
            conclusion_version_id=row["conclusion_version_id"],
            version_number=int(row["version_number"]),
            statement=row["statement"],
            outcome=ConclusionOutcome(row["outcome"]),
            dependency_rule=json.loads(row["dependency_rule_json"]),
            reason_refs=tuple(json.loads(row["reason_refs_json"])),
            reasoned_at=row["reasoned_at"],
            supersedes_conclusion_version_id=row["supersedes_conclusion_version_id"],
        )

    @staticmethod
    def _edge_from_row(row: sqlite3.Row) -> DependencyEdge:
        return DependencyEdge(
            edge_id=row["edge_id"],
            edge_type=EdgeType(row["edge_type"]),
            from_id=row["from_id"],
            to_id=row["to_id"],
            created_at=row["created_at"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            rule_version=row["rule_version"],
        )
