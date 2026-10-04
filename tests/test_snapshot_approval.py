"""Synthetic evidence exercises production snapshot approval, not business acceptance."""

import json
import unittest
from collections import Counter
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests.schema_fixture import synthetic_schema
from text2sql.knowledge.governance import content_digest
from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.knowledge.snapshot import ArtifactSnapshot
from text2sql.knowledge.structured import KnowledgeBundle, load_validated_knowledge_bundle

SCHEMA = synthetic_schema(
    {"Fact": {"columns": {"ID": {"data_type": "int"}, "Amount": {"data_type": "decimal"}}}}
)


def reviewed(record, *, schema=SCHEMA):
    """Fictional independent reviewers bind the actual authored fields and Schema."""
    record = deepcopy(record)
    record["review"] = {
        "source": "synthetic snapshot approval contract test",
        "business_reviewer": "synthetic-business-reviewer",
        "data_reviewer": "synthetic-data-reviewer",
        "reviewed_at": "2026-10-03T00:00:00+00:00",
        "schema_fingerprint": schema_fingerprint(schema),
        "content_sha256": content_digest(record),
        "notes": "Fictional evidence; no production accuracy or business approval claimed.",
    }
    return record


def approved_bundle():
    return KnowledgeBundle(
        table_cards=[
            reviewed(
                {
                    "table": "Fact",
                    "description": "synthetic collection event",
                    "grain": "one synthetic event per row",
                    "important_columns": ["ID", "Amount"],
                }
            )
        ],
        metrics=[
            reviewed(
                {
                    "id": "amount_sum",
                    "name": "synthetic amount",
                    "source_table": "Fact",
                    "aggregation": "SUM",
                    "column": "Amount",
                    "unit": "mL",
                    "output_alias": "Amount",
                }
            )
        ],
    )


def snapshot_bytes(bundle=None, *, schema=SCHEMA):
    payload = ArtifactSnapshot(
        deepcopy(schema), bundle or approved_bundle(), "synthetic-retrieval-fingerprint"
    ).to_dict()
    return json.dumps(payload, ensure_ascii=False).encode()


class SnapshotApprovalTests(unittest.TestCase):
    def assert_rejected(self, content, message):
        for method in ("from_bytes", "load"):
            with self.subTest(method=method):
                with self.assertRaisesRegex(ValueError, message):
                    if method == "from_bytes":
                        ArtifactSnapshot.from_bytes(content, require_reviewed=True)
                    else:
                        with patch.object(Path, "read_bytes", return_value=content) as reader:
                            ArtifactSnapshot.load("isolated-snapshot.json", require_reviewed=True)
                        reader.assert_called_once()

    def test_complete_synthetic_independent_approval_passes_both_entry_points(self):
        content = snapshot_bytes()
        parsed = ArtifactSnapshot.from_bytes(content, require_reviewed=True)
        self.assertEqual(parsed.knowledge.metrics[0]["unit"], "mL")
        self.assertEqual(parsed.knowledge.table_cards[0]["grain"], "one synthetic event per row")
        with patch.object(Path, "read_bytes", return_value=content) as reader:
            loaded = ArtifactSnapshot.load("isolated-snapshot.json", require_reviewed=True)
        self.assertEqual(loaded.to_dict(), parsed.to_dict())
        reader.assert_called_once()

    def test_unreviewed_card_or_metric_is_not_production_knowledge(self):
        for attribute in ("table_cards", "metrics"):
            bundle = approved_bundle()
            getattr(bundle, attribute)[0].pop("review")
            with self.subTest(attribute=attribute):
                self.assert_rejected(snapshot_bytes(bundle), "current Schema review required")
                # Unreviewed inputs remain development data, never production approval.
                ArtifactSnapshot.from_bytes(snapshot_bytes(bundle), require_reviewed=False)

    def test_schema_compatible_record_with_stale_review_is_rejected(self):
        bundle = approved_bundle()
        stale_schema = deepcopy(SCHEMA)
        stale_schema["Fact"]["columns"]["ID"]["is_nullable"] = True
        for attribute in ("table_cards", "metrics"):
            getattr(bundle, attribute)[0]["review"]["schema_fingerprint"] = schema_fingerprint(
                stale_schema
            )
        self.assert_rejected(snapshot_bytes(bundle), "current Schema review required")

    def test_stale_outer_schema_fingerprint_is_rejected(self):
        payload = json.loads(snapshot_bytes())
        payload["schema"]["Fact"]["columns"]["ID"]["is_nullable"] = True
        self.assert_rejected(json.dumps(payload).encode(), "stale schema snapshot")

    def test_missing_fact_grain_cannot_be_approved_by_rehashing(self):
        for value in (None, "omitted", "   "):
            bundle = approved_bundle()
            card = bundle.table_cards[0]
            if value == "omitted":
                card.pop("grain")
            else:
                card["grain"] = value
            bundle.table_cards[0] = reviewed(card)
            with self.subTest(value=value):
                self.assert_rejected(snapshot_bytes(bundle), "reviewed fact grain is required")

    def test_missing_or_raw_units_cannot_be_approved_by_rehashing(self):
        for value in ("数据库原始单位", "数据库原始单位 ", " 数据库原始单位", "   ", "omitted"):
            bundle = approved_bundle()
            metric = bundle.metrics[0]
            if value == "omitted":
                metric.pop("unit")
            else:
                metric["unit"] = value
            bundle.metrics[0] = reviewed(metric)
            with self.subTest(value=value):
                self.assert_rejected(snapshot_bytes(bundle), "authoritative unit is required")

    def test_boolean_snapshot_version_cannot_impersonate_an_integer_protocol(self):
        payload = json.loads(snapshot_bytes())
        payload["schema_version"] = True
        self.assert_rejected(json.dumps(payload).encode(), "invalid knowledge snapshot schema")

    def test_authored_content_changes_invalidate_review_even_with_new_outer_hash(self):
        for attribute, field, value in (
            ("table_cards", "grain", "two synthetic events per row"),
            ("metrics", "aggregation", "AVG"),
            ("metrics", "unit", "L"),
        ):
            bundle = approved_bundle()
            getattr(bundle, attribute)[0][field] = value
            with self.subTest(attribute=attribute, field=field):
                self.assert_rejected(
                    snapshot_bytes(bundle),
                    "review evidence does not match current authored content",
                )

    def test_missing_reviewed_metric_or_incomplete_table_card_coverage_is_rejected(self):
        bundle = approved_bundle()
        bundle.metrics = []
        self.assert_rejected(snapshot_bytes(bundle), "requires reviewed metric definitions")
        bundle = approved_bundle()
        bundle.table_cards = []
        self.assert_rejected(snapshot_bytes(bundle), "reviewed table card for every Schema table")

    def test_load_reads_one_byte_stream_without_reopening_mutating_source(self):
        original = snapshot_bytes()
        mutated_bundle = approved_bundle()
        mutated_bundle.metrics[0]["aggregation"] = "AVG"
        changed = snapshot_bytes(mutated_bundle)
        with patch.object(Path, "read_bytes", side_effect=[original, changed, original]) as reader:
            loaded = ArtifactSnapshot.load("aba-source.json", require_reviewed=True)
        reader.assert_called_once()
        self.assertEqual(loaded.knowledge.metrics[0]["aggregation"], "SUM")


class FrozenKnowledgeReaderTests(unittest.TestCase):
    def test_fixed_reader_binds_source_parse_and_provenance_despite_aba_path(self):
        root = Path("isolated-frozen-knowledge")
        bundle = approved_bundle()
        frozen = {
            str(root / "manifest.json"): b'{"schema_version":1}',
            str(root / "schema/table_cards.jsonl"): (
                json.dumps(bundle.table_cards[0], ensure_ascii=False) + "\n"
            ).encode(),
            str(root / "domain/metrics.json"): json.dumps(bundle.metrics).encode(),
        }
        calls = Counter()
        live_source = deepcopy(frozen)

        def fixed_reader(path):
            key = str(path)
            calls[key] += 1
            # Logical source mutation is deliberately not visible to the frozen reader.
            if calls[key] == 1:
                live_source[key] = b"unreviewed changed bytes"
            elif calls[key] == 2:
                live_source[key] = frozen.get(key)
            return frozen.get(key)

        with patch.object(Path, "read_bytes", side_effect=AssertionError("must use pinned reader")):
            loaded = load_validated_knowledge_bundle(
                root, SCHEMA, require_reviewed=True, reader=fixed_reader
            )
        self.assertEqual(loaded.fingerprint, bundle.fingerprint)
        self.assertEqual(loaded.errors, [])
        self.assertEqual(calls[str(root / "schema/table_cards.jsonl")], 2)
        self.assertEqual(calls[str(root / "domain/metrics.json")], 2)
