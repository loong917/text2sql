"""Export authoritative metadata from an immutable knowledge artifact."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from text2sql.application.context_state import ContextRuntimeState
from text2sql.core.config import Settings, load_settings
from text2sql.infrastructure.query_adapters import VannaSqlExecutor
from text2sql.infrastructure.runtime import RuntimeResources
from text2sql.infrastructure.schema_repository import get_live_schema
from text2sql.knowledge.artifacts import KnowledgeArtifactRegistry
from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.knowledge.schema_contract import require_schema_contract
from text2sql.knowledge.snapshot import ArtifactSnapshot


async def _read_live_schema(config: Settings) -> dict[str, dict[str, Any]]:
    """Own metadata-only SQL resources; never train or load a knowledge artifact."""
    runtime = RuntimeResources(config)
    state = ContextRuntimeState()
    executor: VannaSqlExecutor | None = None
    try:
        executor = VannaSqlExecutor(runtime.sql_runner, max_concurrency=config.sql_max_concurrency)
        return require_schema_contract(
            await get_live_schema(0, force_refresh=True, sql_executor=executor, state=state)
        )
    finally:
        try:
            if executor is not None:
                await executor.aclose()
        finally:
            state.reset()
            await asyncio.to_thread(runtime.close)


def main() -> None:
    parser = argparse.ArgumentParser()
    source_args = parser.add_mutually_exclusive_group()
    source_args.add_argument("--snapshot", help="explicit immutable knowledge_snapshot.json")
    source_args.add_argument(
        "--live", action="store_true", help="read SQL Server system metadata without an artifact"
    )
    parser.add_argument("--output")
    args = parser.parse_args()
    config = load_settings()
    if args.live:
        schema = asyncio.run(_read_live_schema(config))
        source = "sql-server:sys-catalog"
    else:
        source = args.snapshot
        if not source:
            active = KnowledgeArtifactRegistry(
                config.knowledge_artifact_dir, config.knowledge_active_pointer_path
            ).load_active()
            if active is None:
                raise RuntimeError(
                    "no complete active artifact; refusing to overwrite Schema snapshot"
                )
            source = active.snapshot_path
        schema = require_schema_contract(ArtifactSnapshot.load(source).schema)
    payload = {
        "schema_version": f"sha256:{schema_fingerprint(schema)}",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": source if args.live else Path(source).as_posix(),
        "tables": schema,
    }
    output = Path(args.output or config.schema_snapshot_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    from text2sql.knowledge.atomic import atomic_json

    atomic_json(output, payload)
    print(f"exported {len(schema)} tables to {output}")


if __name__ == "__main__":
    main()
