"""Evaluation consumes immutable inputs rather than re-reading a live source path."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.catalog_fixture import TEST_CATALOG
from tests.evaluation_fixture import EVALUATION_SCHEMA, evaluation_cases, plan_diagnostics
from tests.test_production_readiness import production_fixture
from text2sql.domain.sql_validation import SqlSafetyPolicy, limit_tsql_rows
from text2sql.evaluation.artifact_evidence import artifact_file_hashes, freeze_artifact_files
from text2sql.evaluation.service import EvaluationConfig, EvaluationService
from text2sql.evaluation.wiring import run_configured_evaluation


class EvaluationSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_consumes_dataset_bytes_without_reading_replaced_path(self):
        cases = evaluation_cases()
        dataset = ("\n".join(json.dumps(case) for case in cases) + "\n").encode()
        query = AsyncMock()
        query.generate.return_value = {
            "success": True,
            "outcome": "success",
            "sql": cases[0]["baseline_sql"],
            "result": [{"Total": 1}],
            "result_columns": ["Total"],
            "result_row_count": 1,
            "candidate_tables": ["Fact"],
            "diagnostics": plan_diagnostics(cases[0]["question"]),
        }
        executor = AsyncMock()
        executor.execute.return_value = [{"Total": 1}]
        service = EvaluationService(
            query,
            executor,
            TEST_CATALOG,
            EvaluationConfig(
                "dev.jsonl",
                "test.jsonl",
                live_schema=EVALUATION_SCHEMA,
                safety_policy=SqlSafetyPolicy(),
            ),
        )
        with patch(
            "text2sql.evaluation.service.load_evaluation_cases",
            side_effect=AssertionError("must not re-read the source dataset"),
        ):
            results = await service.run("test", dataset_bytes=dataset)
        self.assertEqual([result["id"] for result in results], [case["id"] for case in cases])
        self.assertEqual(
            executor.execute.await_args.args[0], limit_tsql_rows(cases[0]["baseline_sql"], 501)
        )

    async def test_wiring_forwards_the_exact_bytes_and_closes_service(self):
        config = object()
        runtime = object()
        service = AsyncMock()
        service.run.return_value = []
        dataset = b"immutable dataset bytes"
        with patch("text2sql.evaluation.wiring.build_evaluation_service", return_value=service):
            self.assertEqual(
                await run_configured_evaluation(config, runtime, "test", dataset_bytes=dataset), []
            )
        service.run.assert_awaited_once_with("test", dataset_bytes=dataset)
        service.aclose.assert_awaited_once()


class FrozenArtifactFileTests(unittest.TestCase):
    def test_only_controlled_files_are_copied_hashed_and_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            _, artifact, _, _ = production_fixture(Path(directory))
            expected = artifact_file_hashes(artifact)
            with freeze_artifact_files(artifact) as frozen:
                root = Path(frozen.artifact.knowledge_index_path).parent
                self.assertEqual(frozen.hashes, expected)
                self.assertEqual(artifact_file_hashes(frozen.artifact), expected)
                self.assertEqual(
                    {path.name for path in root.iterdir()},
                    {
                        "knowledge_index.json",
                        "table_retrieval_calibrator.json",
                        "training_report.json",
                        "knowledge_snapshot.json",
                        "training_manifest.json",
                    },
                )
                Path(artifact.knowledge_index_path).write_text("modified", encoding="utf-8")
                self.assertEqual(artifact_file_hashes(frozen.artifact), expected)
            self.assertFalse(root.exists())

    def test_exception_cleans_up_isolated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            _, artifact, _, _ = production_fixture(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "evaluation failure"):
                with freeze_artifact_files(artifact) as frozen:
                    root = Path(frozen.artifact.knowledge_index_path).parent
                    raise RuntimeError("evaluation failure")
            self.assertFalse(root.exists())
