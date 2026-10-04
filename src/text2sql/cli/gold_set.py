"""Inspect or export reviewed Gold assets without starting model generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..evaluation.gold_set import (
    GoldCase,
    audit_gold_set,
    export_gold_set,
    load_gold_cases,
    migrate_legacy_sources,
    verify_generation,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gold Set review, isolation and immutable export")
    parser.add_argument("command", choices=("check", "export", "verify", "migrate", "schema"))
    parser.add_argument("--root", default="evaluation/gold_set")
    parser.add_argument(
        "--schema", help="complete schema_snapshot.json exported from real metadata"
    )
    parser.add_argument("--output", help="new, nonexisting generation directory")
    parser.add_argument("--enforce-release", action="store_true")
    parser.add_argument("--legacy-root", default=".")
    parser.add_argument(
        "--quarantine",
        action="store_true",
        help="archive legacy source bytes then empty only derived runtime views",
    )
    args = parser.parse_args()
    if args.command == "schema":
        from ..knowledge.atomic import atomic_json

        payload = GoldCase.model_json_schema()
        if args.output:
            atomic_json(args.output, payload)
        else:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.command == "migrate":
        report = migrate_legacy_sources(args.legacy_root, args.root, quarantine=args.quarantine)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.command == "verify":
        errors = verify_generation(args.root)
        print(json.dumps({"errors": errors, "valid": not errors}, ensure_ascii=False, indent=2))
        if errors:
            raise SystemExit(1)
        return
    cases = load_gold_cases(args.root)
    report = audit_gold_set(cases)
    if args.command == "check":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.enforce_release and not report["production_data_ready"]:
            raise SystemExit(1)
        return
    if not args.schema or not args.output:
        parser.error("export requires --schema and --output")
    from ..core.config import load_settings
    from ..domain.sql_validation import SqlSafetyPolicy
    from ..retrieval.table_card import load_schema_snapshot

    config = load_settings()
    policy = SqlSafetyPolicy(
        allowed_schemas=tuple(
            value.strip() for value in config.sql_allowed_schemas.split(",") if value.strip()
        ),
        allowed_tables=tuple(
            value.strip() for value in config.sql_allowed_tables.split(",") if value.strip()
        ),
        denied_tables=tuple(
            value.strip() for value in config.sql_denied_tables.split(",") if value.strip()
        ),
        denied_columns=tuple(
            value.strip() for value in config.sql_denied_columns.split(",") if value.strip()
        ),
        aggregation_only_tables=tuple(
            value.strip()
            for value in config.sql_aggregation_only_tables.split(",")
            if value.strip()
        ),
        max_joins=config.sql_max_joins,
        max_subqueries=config.sql_max_subqueries,
        allow_select_star=config.sql_allow_select_star,
        require_table=config.sql_require_table,
        allow_cross_join=config.sql_allow_cross_join,
    )
    output = export_gold_set(
        args.root,
        Path(args.output),
        load_schema_snapshot(args.schema),
        policy,
        knowledge_root=config.structured_knowledge_dir,
    )
    print(f"exported immutable reviewed Gold generation to {output}")


if __name__ == "__main__":
    main()
