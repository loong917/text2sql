"""A diagnostic evaluation report must not overwrite its own inputs."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_production_readiness import production_fixture
from text2sql.evaluation.run import main


class EvaluationOutputPolicyTests(unittest.TestCase):
    def test_dev_output_rejects_sources_artifacts_and_non_json_files_before_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            config, artifact, _, _ = production_fixture(Path(directory))
            targets = (
                config.eval_dev_set_path,
                config.eval_test_set_path,
                config.retrieval_train_set_path,
                config.retrieval_calibration_set_path,
                config.retrieval_test_set_path,
                config.table_retrieval_calibrator_path,
                config.schema_snapshot_path,
                config.training_state_path,
                config.feedback_db_path,
                config.eval_test_report_path,
                config.production_readiness_report_path,
                config.production_release_manifest_path,
                config.knowledge_active_pointer_path,
                str(Path(config.structured_knowledge_dir) / "domain/metrics.json"),
                str(Path(config.eval_test_set_path).parent.parent / "gold-manifest.json"),
                artifact.snapshot_path,
                str(Path(config.knowledge_db_dir) / "segment/index.json"),
                str(Path(directory) / "source.py"),
            )
            for target in targets:
                with self.subTest(target=target):
                    path = Path(target)
                    before = path.read_bytes() if path.is_file() else None
                    with (
                        patch("sys.argv", ["evaluate", "--split", "dev", "--output", str(path)]),
                        patch("text2sql.core.config.load_settings", return_value=config),
                        patch("text2sql.infrastructure.runtime.RuntimeResources") as runtime,
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit) as error,
                    ):
                        main()
                    self.assertEqual(error.exception.code, 2)
                    runtime.assert_not_called()
                    self.assertEqual(path.read_bytes() if path.is_file() else None, before)
