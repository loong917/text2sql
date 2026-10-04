import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from tests.schema_fixture import synthetic_schema
from text2sql.knowledge.artifacts import KnowledgeArtifactRegistry
from text2sql.knowledge.models import TableCardRecord
from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.knowledge.snapshot import ArtifactSnapshot
from text2sql.knowledge.structured import KnowledgeBundle, KnowledgeValidationError


def _complete_candidate(registry: KnowledgeArtifactRegistry, name: str = "schema"):
    schema = synthetic_schema({"Fact": {"columns": {"ID": {}}, "fixture": name}})
    candidate = registry.candidate(schema_fingerprint=schema_fingerprint(schema))
    for value in (
        candidate.knowledge_index_path,
        candidate.calibrator_path,
        candidate.manifest_path,
        candidate.report_path,
    ):
        path = Path(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]" if path.name == "knowledge_index.json" else "{}", encoding="utf-8")
    Path(candidate.snapshot_path).write_text(
        json.dumps(ArtifactSnapshot(schema, KnowledgeBundle(), "dataset").to_dict()),
        encoding="utf-8",
    )
    return candidate


class KnowledgeArtifactTests(unittest.TestCase):
    def test_snapshot_rejects_self_consistently_hashed_unknown_business_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge_snapshot.json"
            bundle = KnowledgeBundle(
                table_cards=[
                    TableCardRecord(table="Unknown", description="not in schema").model_dump()
                ]
            )
            path.write_text(
                json.dumps(
                    ArtifactSnapshot(
                        synthetic_schema({"Fact": {"columns": {"ID": {}}}}), bundle, "dataset"
                    ).to_dict()
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(KnowledgeValidationError, "unknown table"):
                ArtifactSnapshot.load(path)

    def test_active_pointer_requires_complete_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = KnowledgeArtifactRegistry(root / "artifacts", root / "active.json")
            candidate = registry.candidate(schema_fingerprint="schema")
            Path(candidate.knowledge_index_path).parent.mkdir(parents=True)
            Path(candidate.knowledge_index_path).write_text("[]", encoding="utf-8")
            registry.publish(candidate)
            self.assertIsNone(registry.load_active())

    def test_training_lease_never_steals_an_aged_owner_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = KnowledgeArtifactRegistry(root / "artifacts", root / "active.json")
            lease = registry.acquire_training_lease(stale_seconds=60)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                registry.acquire_training_lease(stale_seconds=60)
            lease.release()

            stale = registry.root / ".training.lock"
            stale.write_text(json.dumps({"token": "stale"}), encoding="utf-8")
            old = time.time() - 120
            os.utime(stale, (old, old))
            with self.assertRaisesRegex(RuntimeError, "verify owner termination"):
                registry.acquire_training_lease(stale_seconds=60)
            self.assertTrue(stale.exists())
            self.assertEqual(json.loads(stale.read_bytes()), {"token": "stale"})

    def test_prune_retains_active_and_newest_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = KnowledgeArtifactRegistry(root / "artifacts", root / "active.json")
            candidates = []
            for index in range(4):
                candidate = _complete_candidate(registry, str(index))
                timestamp = time.time() + index
                os.utime(Path(candidate.knowledge_index_path).parent, (timestamp, timestamp))
                candidates.append(candidate)
            registry.publish(candidates[0])

            deleted_collections = []
            removed = registry.prune(
                retain_count=2,
                delete_collection=deleted_collections.append,
            )

            self.assertTrue(Path(candidates[0].knowledge_index_path).parent.exists())
            self.assertTrue(Path(candidates[-1].knowledge_index_path).parent.exists())
            self.assertEqual(len(removed), 1)
            self.assertEqual(
                deleted_collections,
                [f"knowledge_{removed[0].lower()}"],
            )

    def test_active_pointer_cannot_reference_files_outside_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = KnowledgeArtifactRegistry(root / "artifacts", root / "active.json")
            candidate = _complete_candidate(registry)
            payload = candidate.__dict__ | {"report_path": str(root / "outside.json")}
            (root / "outside.json").write_text("{}", encoding="utf-8")
            registry.active_pointer.write_text(json.dumps(payload), encoding="utf-8")

            self.assertIsNone(registry.load_active())


if __name__ == "__main__":
    unittest.main()
