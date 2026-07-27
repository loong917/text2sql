"""Command-line runner for development and frozen test evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated Text2SQL evaluation")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--output", help="optional JSON report path")
    args = parser.parse_args()

    # Delayed import avoids initializing runtime dependencies for --help.
    from ..training.pipeline import run_evaluation_suite

    results = asyncio.run(run_evaluation_suite(args.split))
    payload = {
        "split": args.split,
        "total": len(results),
        "passed": sum(1 for item in results if item.get("passed")),
        "failed": sum(1 for item in results if not item.get("passed")),
        "results": results,
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print(f"evaluation report written to {output}")
    else:
        print(rendered)
