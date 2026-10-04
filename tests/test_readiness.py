"""Readiness must identify local knowledge failures before accepting traffic."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.schema_fixture import synthetic_schema
from text2sql.api.health import build_readiness
from text2sql.core.config import load_settings
from text2sql.knowledge.artifacts import KnowledgeArtifactRegistry
from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.knowledge.snapshot import ArtifactSnapshot
from text2sql.knowledge.structured import KnowledgeBundle


class _Runtime:
    def probe_knowledge_collection(self, _collection_name):
        raise RuntimeError("collection missing")

    def probe_ollama(self):
        return None

    def status(self):
        return {}


class _Executor:
    async def execute(self, _sql, *, timeout_seconds):
        return [{"ready": 1, "timeout": timeout_seconds}]


class _Container:
    runtime = _Runtime()
    sql_executor = _Executor()


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_active_collection_keeps_service_unready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = KnowledgeArtifactRegistry(root / "artifacts", root / "active.json")
            schema = synthetic_schema({"Fact": {"columns": {"ID": {}}}})
            artifact = registry.candidate(schema_fingerprint=schema_fingerprint(schema))
            for path_value, payload in (
                (artifact.knowledge_index_path, "[]"),
                (artifact.calibrator_path, "{}"),
                (artifact.manifest_path, "{}"),
                (artifact.report_path, "{}"),
                (
                    artifact.snapshot_path,
                    json.dumps(ArtifactSnapshot(schema, KnowledgeBundle(), "dataset").to_dict()),
                ),
            ):
                path = Path(path_value)
                path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_text, payload, encoding="utf-8")
            registry.publish(artifact)
            config = replace(
                load_settings(),
                knowledge_artifact_dir=str(root / "artifacts"),
                knowledge_active_pointer_path=str(root / "active.json"),
                table_retrieval_require_calibration=False,
            )

            payload, status = await build_readiness(config, _Container())

            self.assertEqual(status, 503)
            self.assertEqual(payload["checks"]["knowledge_collection"], "missing_or_unreadable")
            self.assertEqual(payload["checks"]["database"], "ready")
            self.assertEqual(payload["checks"]["ollama"], "ready")


if __name__ == "__main__":
    unittest.main()
