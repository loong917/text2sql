import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from src.knowledge.artifacts import KnowledgeArtifactRegistry


def _complete_candidate(registry: KnowledgeArtifactRegistry, name: str = "schema"):
    candidate = registry.candidate(schema_fingerprint=name)
    for value in (
        candidate.knowledge_index_path,
        candidate.calibrator_path,
        candidate.manifest_path,
        candidate.report_path,
    ):
        path = Path(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]" if path.name == "knowledge_index.json" else "{}", encoding="utf-8")
    return candidate


class KnowledgeArtifactTests(unittest.TestCase):
    def test_active_pointer_requires_complete_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = KnowledgeArtifactRegistry(root / "artifacts", root / "active.json")
            candidate = registry.candidate(schema_fingerprint="schema")
            Path(candidate.knowledge_index_path).parent.mkdir(parents=True)
            Path(candidate.knowledge_index_path).write_text("[]", encoding="utf-8")
            registry.publish(candidate)
            self.assertIsNone(registry.load_active())

    def test_training_lease_rejects_concurrent_owner_and_recovers_stale_lock(self):
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
            recovered = registry.acquire_training_lease(stale_seconds=60)
            recovered.release()
            self.assertFalse(stale.exists())

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
