"""Command-line runner for development and frozen test evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated Text2SQL evaluation")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--output", help="optional JSON report path")
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
    from .reporting import evaluate_quality_gate, summarize_evaluation
    from .wiring import run_configured_evaluation

    settings = load_settings()
    runtime = RuntimeResources(settings)
    try:
        model_digests = runtime.model_digests()
        results = asyncio.run(run_configured_evaluation(settings, runtime, args.split))
    finally:
        runtime.close()
    summary = summarize_evaluation(results)
    gate = evaluate_quality_gate(
        summary,
        min_pass_rate=settings.eval_min_pass_rate,
        min_positive_pass_rate=settings.eval_min_positive_pass_rate,
        min_refusal_pass_rate=settings.eval_min_refusal_pass_rate,
        min_cases=settings.eval_min_cases,
        min_positive_cases=settings.eval_min_positive_cases,
        min_refusal_cases=settings.eval_min_refusal_cases,
        min_semantic_ir_pass_rate=settings.eval_min_semantic_ir_pass_rate,
        min_execution_pass_rate=settings.eval_min_execution_pass_rate,
        min_retrieval_recall=settings.eval_min_retrieval_recall,
    )
    artifact = KnowledgeArtifactRegistry(
        settings.knowledge_artifact_dir,
        settings.knowledge_active_pointer_path,
    ).load_active()
    identity = release_identity(settings)
    identity["observed_model_digests"] = model_digests
    dataset_path = (
        settings.eval_test_set_path if args.split == "test" else settings.eval_dev_set_path
    )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "split": args.split,
        "attestation": {
            "artifact_version": artifact.version if artifact else None,
            "schema_fingerprint": artifact.schema_fingerprint if artifact else None,
            "dataset_sha256": file_sha256(dataset_path),
            "release_identity": identity,
        },
        "summary": summary,
        "quality_gate": gate,
        "results": results,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    output_value = args.output or (settings.eval_test_report_path if args.split == "test" else None)
    if output_value:
        output = Path(output_value)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"evaluation report written to {output}")
    else:
        print(rendered)
    if args.enforce_gate and not gate["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
