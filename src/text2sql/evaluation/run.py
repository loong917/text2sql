"""Command-line runner for development and frozen test evaluation."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated Text2SQL evaluation")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--artifact", help="registered candidate version; does not change ACTIVE")
    parser.add_argument("--output", help="Dev report path; Test reports are version-owned")
    parser.add_argument(
        "--enforce-gate",
        action="store_true",
        help="exit non-zero when configured release thresholds are not met",
    )
    args = parser.parse_args()

    # Delayed import avoids initializing runtime dependencies for --help.
    from ..core.build_info import file_sha256, release_identity
    from ..core.config import load_settings
    from ..infrastructure.runtime import RuntimeResources
    from ..knowledge.artifacts import KnowledgeArtifactRegistry
    from ..knowledge.atomic import atomic_json
    from ..knowledge.collection_evidence import assert_collection_evidence
    from .artifact_evidence import artifact_file_hashes, freeze_artifact_files
    from .dataset import load_evaluation_cases_bytes
    from .report_contract import EVALUATION_REPORT_VERSION, validate_evaluation_report
    from .reporting import evaluate_configured_quality_gate, summarize_evaluation
    from .wiring import run_configured_evaluation

    settings = load_settings()
    registry = KnowledgeArtifactRegistry(
        settings.knowledge_artifact_dir, settings.knowledge_active_pointer_path
    )
    artifact = registry.load_version(args.artifact) if args.artifact else registry.load_active()
    if artifact is None:
        raise RuntimeError("evaluation requires a complete registered knowledge artifact")
    report_path = registry.test_report_path(artifact) if args.split == "test" else None
    if args.split == "test" and registry.release_path(artifact).exists():
        raise RuntimeError(
            "approved artifacts are immutable; build a new candidate before Test evaluation"
        )
    if (
        args.output
        and args.split == "test"
        and report_path is not None
        and Path(args.output).resolve() != report_path.resolve()
    ):
        parser.error("Test reports must use the selected artifact's test_report.json")
    if args.output and args.split == "dev":
        output_path = Path(args.output).resolve()
        protected = {
            Path(value).resolve()
            for value in (
                settings.eval_test_set_path,
                settings.eval_dev_set_path,
                settings.retrieval_train_set_path,
                settings.retrieval_calibration_set_path,
                settings.retrieval_test_set_path,
                settings.table_retrieval_calibrator_path,
                settings.schema_snapshot_path,
                settings.training_state_path,
                settings.feedback_db_path,
                settings.eval_test_report_path,
                settings.production_readiness_report_path,
                settings.production_release_manifest_path,
                Path(settings.eval_test_set_path).parent.parent / "gold-manifest.json",
                Path(settings.eval_test_set_path).parent.parent / "gold-cases.jsonl",
                registry.active_pointer,
            )
            if value
        }
        if (
            output_path.suffix.lower() != ".json"
            or output_path in protected
            or any(
                output_path.is_relative_to(root)
                for root in (
                    registry.root.resolve(),
                    Path(settings.structured_knowledge_dir).resolve(),
                    Path(settings.knowledge_db_dir).resolve(),
                    Path(__file__).resolve().parents[1],
                )
            )
        ):
            parser.error("Dev output must be a separate JSON report, not an input or artifact")
        report_path = output_path
    dataset_path = (
        settings.eval_test_set_path if args.split == "test" else settings.eval_dev_set_path
    )
    dataset_bytes = Path(dataset_path).read_bytes()
    dataset_hash = hashlib.sha256(dataset_bytes).hexdigest()
    cases = load_evaluation_cases_bytes(
        dataset_bytes, source=dataset_path, expected_split=args.split
    )
    if not cases:
        raise ValueError(f"evaluation split '{args.split}' is empty")
    identity = release_identity(settings)
    lease = registry.acquire_training_lease(
        stale_seconds=settings.knowledge_training_lock_stale_seconds
    )
    try:
        runtime = RuntimeResources(settings)
    except BaseException:
        lease.release()
        raise
    try:
        if args.split == "test" and registry.release_path(artifact).exists():
            raise RuntimeError(
                "approved artifacts are immutable; build a new candidate before Test evaluation"
            )
        with freeze_artifact_files(artifact) as frozen:
            artifact_hashes = frozen.hashes
            manifest = json.loads(Path(frozen.artifact.manifest_path).read_bytes())
            index = json.loads(Path(frozen.artifact.knowledge_index_path).read_bytes())
            collection_evidence = manifest.get("outputs", {}).get("collection_evidence")
            assert_collection_evidence(
                runtime.collection_evidence(artifact.collection_name, index_records=index),
                collection_evidence,
            )
            runtime.probe_ollama()
            model_digests = runtime.model_digests()
            results = asyncio.run(
                run_configured_evaluation(
                    settings,
                    runtime,
                    args.split,
                    dataset_bytes=dataset_bytes,
                    knowledge_memory=runtime.create_knowledge_memory(
                        collection_name=artifact.collection_name
                    ),
                    knowledge_index_path=frozen.artifact.knowledge_index_path,
                    calibrator_path=frozen.artifact.calibrator_path,
                )
            )
            if runtime.model_digests() != model_digests:
                raise RuntimeError("Ollama model identities changed during evaluation")
        summary = summarize_evaluation(results)
        gate = evaluate_configured_quality_gate(summary, settings)
        current = (
            registry.load_version(artifact.version) if args.artifact else registry.load_active()
        )
        if current != artifact or artifact_file_hashes(artifact) != artifact_hashes:
            raise RuntimeError(
                "selected artifact changed during evaluation; rerun against a frozen version"
            )
        if file_sha256(dataset_path) != dataset_hash or release_identity(settings) != identity:
            raise RuntimeError(
                "evaluation inputs changed during the run; evidence was not published"
            )
        identity["observed_model_digests"] = model_digests
        payload = {
            "schema_version": EVALUATION_REPORT_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "split": args.split,
            "attestation": {
                "artifact_version": artifact.version if artifact else None,
                "schema_fingerprint": artifact.schema_fingerprint if artifact else None,
                "dataset_sha256": dataset_hash,
                "artifact_sha256": artifact_hashes,
                "release_identity": identity,
            },
            "summary": summary,
            "quality_gate": gate,
            "results": results,
        }
        _, evidence_errors = validate_evaluation_report(
            payload,
            cases,
            expected_split=args.split,
        )
        if evidence_errors:
            raise RuntimeError("evaluation evidence is invalid: " + "; ".join(evidence_errors))
        current = (
            registry.load_version(artifact.version) if args.artifact else registry.load_active()
        )
        if (
            current != artifact
            or artifact_file_hashes(artifact) != artifact_hashes
            or file_sha256(dataset_path) != dataset_hash
            or release_identity(settings)
            != {key: value for key, value in identity.items() if key != "observed_model_digests"}
        ):
            raise RuntimeError(
                "evaluation inputs changed before publication; evidence was not published"
            )
        assert_collection_evidence(
            runtime.collection_evidence(artifact.collection_name, index_records=index),
            collection_evidence,
        )
        if runtime.model_digests() != model_digests:
            raise RuntimeError("Ollama model identities changed before publication")
        if args.split == "test" and registry.release_path(artifact).exists():
            raise RuntimeError(
                "artifact was approved during evaluation; its Test evidence is immutable"
            )
        if report_path is not None:
            atomic_json(report_path, payload)
            print(f"evaluation report written to {report_path}")
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.enforce_gate and not gate["passed"]:
            raise SystemExit(1)
    finally:
        try:
            runtime.close()
        finally:
            lease.release()


if __name__ == "__main__":
    main()
