"""Export an offline Schema snapshot from the current sidecar index."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from src.knowledge.provenance import schema_fingerprint
from src.retrieval.table_card import rebuild_schema_from_knowledge_index


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default="vanna_knowledge_db/knowledge_index.json")
    parser.add_argument("--output", default="knowledge/schema/schema_snapshot.json")
    args = parser.parse_args()
    schema = rebuild_schema_from_knowledge_index(args.index)
    payload = {
        "schema_version": f"sha256:{schema_fingerprint(schema)}",
        "generated_at": datetime.now(UTC).isoformat(),
        "source": Path(args.index).as_posix(),
        "tables": schema,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"exported {len(schema)} tables to {output}")


if __name__ == "__main__":
    main()
