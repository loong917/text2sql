"""Manifest digests do not replace canonical derivation or machine-file validation."""

import json
from collections import Counter
from hashlib import sha256
from pathlib import Path

import pytest

from tests.evaluation_fixture import (
    EVALUATION_SCHEMA,
    evaluation_split_cases,
    reviewed_evaluation_case,
)
from tests.test_gold_set import canonical_case, negative_case, write_cases
from text2sql.domain.sql_validation import SqlSafetyPolicy
from text2sql.evaluation.gold_set import (
    EXPORT_PATHS,
    KNOWLEDGE_FILES,
    assert_training_isolation,
    audit_gold_set,
    export_gold_set,
    verify_generation,
)


def generation(tmp_path):
    root, output = tmp_path / "canonical", tmp_path / "generation"
    write_cases(root, [canonical_case()])
    export_gold_set(root, output, EVALUATION_SCHEMA, SqlSafetyPolicy())
    return output


def replace_view_and_digest(root, relative, data):
    (root / relative).write_bytes(data)
    path = root / "gold-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"][relative] = sha256(data).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize(
    "tampering", ["invalid_json", "candidate", "wrong_split", "different_approved"]
)
def test_rehashing_changed_views_cannot_break_canonical_correspondence(tmp_path, tampering):
    root = generation(tmp_path)
    payload = evaluation_split_cases("test")[0]
    if tampering == "invalid_json":
        data = b"not valid JSON\n"
    else:
        if tampering == "candidate":
            payload["status"] = "candidate"
        elif tampering == "wrong_split":
            payload = evaluation_split_cases("dev")[0]
        else:
            payload.update(id="other-approved-id", question="another separately reviewed case")
            payload = reviewed_evaluation_case(payload)
        data = (json.dumps(payload) + "\n").encode()
    replace_view_and_digest(root, "evaluation/test.jsonl", data)
    errors = verify_generation(root)
    assert any("does not match approved canonical" in error for error in errors)
    if tampering != "different_approved":
        assert any("evaluation/test.jsonl:" in error for error in errors)


def test_all_candidate_canonical_is_not_a_valid_generation_even_with_matching_empty_views(tmp_path):
    root = generation(tmp_path)
    candidate = canonical_case(candidate=True)
    canonical = (candidate.model_dump_json() + "\n").encode()
    (root / "gold-cases.jsonl").write_bytes(canonical)
    path = root / "gold-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest.update(
        canonical_sha256=sha256(canonical).hexdigest(), audit=audit_gold_set([candidate])
    )
    for relative in EXPORT_PATHS.values():
        (root / relative).write_bytes(b"")
        manifest["files"][relative] = sha256(b"").hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert any("reviewed approved final-purpose" in error for error in verify_generation(root))


def test_rehashed_invalid_knowledge_files_are_not_contract_valid(tmp_path):
    root = generation(tmp_path)
    for relative in KNOWLEDGE_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_view_and_digest(root, relative, b"not valid JSON\n")
    errors = verify_generation(root, require_knowledge=True)
    assert any("knowledge/manifest.json:" in error for error in errors)
    assert any("knowledge/domain/metrics.json:" in error for error in errors)


def test_duplicate_payload_ids_with_distinct_canonical_ids_are_rejected_before_export(tmp_path):
    first = canonical_case()
    second = first.model_copy(update={"id": "distinct-canonical-id"})
    root, output = tmp_path / "canonical", tmp_path / "generation"
    write_cases(root, [first, second])
    assert audit_gold_set([first, second])["approved_contract_errors"]
    with pytest.raises(ValueError, match="duplicate"):
        export_gold_set(root, output, EVALUATION_SCHEMA, SqlSafetyPolicy())
    assert not output.exists()


def test_verifier_reads_each_file_once_from_the_supplied_snapshot(tmp_path):
    root = generation(tmp_path)
    frozen = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    calls = Counter()

    def reader(path):
        path = Path(path)
        calls[path] += 1
        return frozen.get(path) if calls[path] == 1 else b"changed on second read"

    assert verify_generation(root, reader=reader) == []
    assert set(calls.values()) == {1}


def test_training_isolation_uses_supplied_bytes_not_live_paths():
    splits = ("retrieval_train", "retrieval_calibration", "retrieval_test", "dev", "test")
    paths = {split: f"nonexistent/{split}.jsonl" for split in splits}
    frozen = {
        paths[split]: (
            "\n".join(json.dumps(case) for case in evaluation_split_cases(split)) + "\n"
        ).encode()
        for split in splits
    }
    assert_training_isolation(paths, [], reader=lambda path: frozen.get(str(path)))
    held_out = evaluation_split_cases("test")[0]
    with pytest.raises(ValueError, match="leakage"):
        assert_training_isolation(
            paths,
            [{"question": held_out["question"], "sql": held_out["baseline_sql"]}],
            reader=lambda path: frozen.get(str(path)),
        )


def test_rehashed_negative_view_and_canonical_cannot_hide_test_template_leakage(tmp_path):
    root = generation(tmp_path)
    held_out = canonical_case()
    negative = negative_case(held_out.payload["baseline_sql"], error_types=["syntax_error"])
    canonical = (held_out.model_dump_json() + "\n" + negative.model_dump_json() + "\n").encode()
    (root / "gold-cases.jsonl").write_bytes(canonical)
    replace_view_and_digest(
        root,
        "knowledge/examples/negative_sql.jsonl",
        (json.dumps(negative.export_payload()) + "\n").encode(),
    )
    path = root / "gold-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest.update(canonical_sha256=sha256(canonical).hexdigest())
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert any("sql_template leakage" in error for error in verify_generation(root))


@pytest.mark.parametrize("error_types", [["semantic_error"], ["syntax_error"]])
def test_training_isolation_checks_approved_negative_templates_from_fixed_bytes(error_types):
    splits = ("retrieval_train", "retrieval_calibration", "retrieval_test", "dev", "test")
    paths = {split: f"nonexistent/{split}.jsonl" for split in splits}
    frozen = {
        paths[split]: (
            "\n".join(json.dumps(case) for case in evaluation_split_cases(split)) + "\n"
        ).encode()
        for split in splits
    }
    negative = negative_case(
        "SELECT COUNT(*) AS Alias FROM dbo.Fact", error_types=error_types
    ).export_payload()
    with pytest.raises(ValueError, match="SQL template leakage"):
        assert_training_isolation(
            paths, [], negative_records=[negative], reader=lambda path: frozen.get(str(path))
        )
    for status in ("candidate", "rejected"):
        assert_training_isolation(
            paths,
            [],
            negative_records=[negative | {"status": status}],
            reader=lambda path: frozen.get(str(path)),
        )
    syntax_negative = negative_case("SELECT (", error_types=["syntax_error"]).export_payload()
    assert_training_isolation(
        paths, [], negative_records=[syntax_negative], reader=lambda path: frozen.get(str(path))
    )
