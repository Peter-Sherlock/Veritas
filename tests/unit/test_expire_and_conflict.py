from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from veritas.domain.enums import ChangeType
from veritas.domain.models import EvidenceSpan, SourceVersion
from veritas.evaluation.scenario import build_change_package, initialize_t0, load_scenario
from veritas.invalidation.repair import EvolutionEngine
from veritas.storage.sqlite import SQLiteRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GS004_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-004" / "scenario.json"
GS005_PATH = PROJECT_ROOT / "datasets" / "scenarios" / "GS-005" / "scenario.json"


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _source(source_id: str, version_id: str, *, supersedes: str | None = None) -> SourceVersion:
    return SourceVersion(
        source_id=source_id,
        version_id=version_id,
        version_label="x",
        canonical_uri=f"fixture://{source_id.lower()}",
        content_hash=_digest(version_id),
        published_at=None,
        observed_at="2026-09-02T00:00:00Z",
        valid_from="2026-09-02T00:00:00Z",
        valid_to=None,
        supersedes_version_id=supersedes,
    )


def _evidence(evidence_id: str, source_version_id: str) -> EvidenceSpan:
    return EvidenceSpan(
        evidence_id=evidence_id,
        source_version_id=source_version_id,
        locator={"section": "x"},
        text="x",
        text_hash=_digest(evidence_id),
        normalized_assertion="x",
        valid_from="2026-09-02T00:00:00Z",
        valid_to=None,
    )


class ExpireValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.repository = SQLiteRepository(root / "veritas.sqlite3")
        self.scenario = load_scenario(GS004_PATH)
        initialize_t0(self.repository, self.scenario)
        self.package = build_change_package(self.scenario)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary_directory.cleanup()

    def test_fixture_is_an_expire_event(self) -> None:
        self.assertEqual(ChangeType.EXPIRE, self.package.event.change_type)

    def test_expire_rejects_new_source(self) -> None:
        package = replace(
            self.package,
            event=replace(
                self.package.event, new_source_version_id="SRC_PREVIEW_PROGRAM@2.0"
            ),
            new_source=_source("SRC_PREVIEW_PROGRAM", "SRC_PREVIEW_PROGRAM@2.0"),
        )
        with self.assertRaises(ValueError):
            EvolutionEngine(self.repository).apply(package)

    def test_expire_rejects_new_evidence(self) -> None:
        package = replace(
            self.package,
            new_evidence=(_evidence("EV_SNEAKY@1", "SRC_PREVIEW_PROGRAM@1.0"),),
        )
        with self.assertRaises(ValueError):
            EvolutionEngine(self.repository).apply(package)


class ConflictValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.repository = SQLiteRepository(root / "veritas.sqlite3")
        self.scenario = load_scenario(GS005_PATH)
        initialize_t0(self.repository, self.scenario)
        self.package = build_change_package(self.scenario)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary_directory.cleanup()

    def test_fixture_is_a_conflict_event(self) -> None:
        self.assertEqual(ChangeType.CONFLICT, self.package.event.change_type)

    def test_conflict_requires_new_source(self) -> None:
        package = replace(self.package, new_source=None)
        with self.assertRaises(ValueError):
            EvolutionEngine(self.repository).apply(package)

    def test_conflict_rejects_superseding_source(self) -> None:
        package = replace(
            self.package,
            new_source=replace(
                self.package.new_source,
                supersedes_version_id="SRC_RUNTIME_GUIDE@1.1",
            ),
        )
        with self.assertRaises(ValueError):
            EvolutionEngine(self.repository).apply(package)

    def test_conflict_rejects_same_source_identity(self) -> None:
        package = replace(
            self.package,
            new_source=replace(self.package.new_source, source_id="SRC_RUNTIME_GUIDE"),
        )
        with self.assertRaises(ValueError):
            EvolutionEngine(self.repository).apply(package)

    def test_conflict_requires_new_edges(self) -> None:
        package = replace(self.package, new_edges=())
        with self.assertRaises(ValueError):
            EvolutionEngine(self.repository).apply(package)

    def test_conflict_provenance_shape_is_accepted(self) -> None:
        EvolutionEngine(self.repository).apply(self.package)
        self.assertEqual([], self.repository.validate_provenance())


if __name__ == "__main__":
    unittest.main()
