"""Export a deterministic offline Schema snapshot from the current sidecar index."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from src.retrieval.table_card import load_schema_snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--index", default="vanna_knowledge_db/knowledge_index.json"
    )
    parser.add_argument(
        "--output", default="knowledge/schema/schema_snapshot.json"
    )
    args = parser.parse_args()
    schema = load_schema_snapshot(args.index)
    canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    payload = {
        "schema_version": "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(Path(args.index).resolve()),
        "tables": schema,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"exported {len(schema)} tables to {output}")


if __name__ == "__main__":
    main()
