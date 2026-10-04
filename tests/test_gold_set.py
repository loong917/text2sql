"""Governed test fixtures cannot stand in for real, reviewed production data."""

import json
from hashlib import sha256
from pathlib import Path

import pytest

from tests.evaluation_fixture import EVALUATION_SCHEMA, evaluation_split_cases, synthetic_review
from text2sql.domain.sql_validation import SqlSafetyPolicy
from text2sql.evaluation.gold_set import (
    GoldCase,
    audit_gold_set,
    export_gold_set,
    isolation_errors,
    load_gold_cases,
    migrate_legacy_sources,
    validate_approved_cases,
    verify_generation,
)


def canonical_case(split="test", *, candidate=False):
    payload = evaluation_split_cases(split)[0]
    return GoldCase(
        id=f"canonical-{split}",
        question=payload["question"],
        purpose=split,
        query_family_id=payload["template_id"],
        category="synthetic",
        difficulty="easy",
        source="synthetic.jsonl",
        source_line=1,
        source_sha256="a" * 64,
        status="candidate" if candidate else "approved",
        payload=payload,
        review=None if candidate else payload["review"],
        execution=None if candidate else payload["execution"],
    )


def write_cases(root, cases):
    root.mkdir()
    (root / "cases.jsonl").write_text(
        "".join(case.model_dump_json() + "\n" for case in cases),
        encoding="utf-8",
    )


def negative_case(sql="SELECT Value FROM Fact", *, error_types=None):
    payload = {
        "id": "negative-example",
        "question": "separately authored negative question",
        "sql": sql,
        "error_types": error_types if error_types is not None else ["semantic_error"],
        "status": "approved",
    }
    return GoldCase(
        id="canonical-negative",
        question=payload["question"],
        purpose="negative",
        query_family_id="separate-negative-family",
        category="synthetic",
        difficulty="easy",
        source="synthetic.jsonl",
        source_line=1,
        source_sha256="a" * 64,
        status="approved",
        payload=payload,
        review=synthetic_review(content=payload),
    )


def test_candidate_is_not_silently_exported(tmp_path):
    root, output = tmp_path / "gold", tmp_path / "export"
    write_cases(root, [canonical_case(candidate=True)])
    assert audit_gold_set(load_gold_cases(root))["approved_purposes"] == {}
    with pytest.raises(ValueError, match="no reviewed approved"):
        export_gold_set(root, output, EVALUATION_SCHEMA, SqlSafetyPolicy())
    assert not output.exists()


def test_approved_requires_independent_review_and_exact_execution():
    case = canonical_case().model_dump()
    for mutation in (
        {"review": None},
        {"execution": None},
        {"review": case["review"] | {"data_reviewer": case["review"]["business_reviewer"]}},
        {"execution": case["execution"] | {"baseline_sha256": "b" * 64}},
        {"execution": case["execution"] | {"truncated": True}},
        {"execution": case["execution"] | {"success": 1}},
        {"execution": case["execution"] | {"truncated": 0}},
    ):
        with pytest.raises(ValueError):
            GoldCase.model_validate(case | mutation)


def test_exports_are_immutable_digest_bound_and_not_production_ready(tmp_path):
    root, output = tmp_path / "gold", tmp_path / "export"
    write_cases(root, [canonical_case()])
    export_gold_set(root, output, EVALUATION_SCHEMA, SqlSafetyPolicy())
    assert verify_generation(output) == []
    assert verify_generation(output, require_knowledge=True)
    assert not audit_gold_set(load_gold_cases(root))["production_data_ready"]
    with pytest.raises(FileExistsError):
        export_gold_set(root, output, EVALUATION_SCHEMA, SqlSafetyPolicy())
    (output / "evaluation/test.jsonl").write_text("changed", encoding="utf-8")
    assert any("changed" in error for error in verify_generation(output))


def test_question_synonyms_cannot_hide_sql_shape_leakage():
    first, second = canonical_case("retrieval_train"), canonical_case("dev")
    second = second.model_copy(
        update={
            "question": "another spelling",
            "query_family_id": "different-label",
            "payload": first.payload | {"question": "another spelling"},
        }
    )
    assert any("sql_template leakage" in error for error in isolation_errors([first, second]))


@pytest.mark.parametrize("error_types", [["semantic_error"], ["syntax_error"]])
def test_parseable_negative_cannot_leak_test_template_even_with_syntax_label(tmp_path, error_types):
    held_out = canonical_case()
    negative = negative_case(
        "SELECT COUNT(*) AS DifferentLabel FROM dbo.Fact", error_types=error_types
    )
    cases = [negative, held_out]
    assert any("sql_template leakage" in error for error in isolation_errors(cases))
    root, output = tmp_path / "gold", tmp_path / "export"
    write_cases(root, cases)
    with pytest.raises(ValueError, match="sql_template leakage"):
        export_gold_set(root, output, EVALUATION_SCHEMA, SqlSafetyPolicy())
    assert not output.exists()


def test_parser_confirmed_syntax_negative_exports_but_still_checks_question_and_family(tmp_path):
    negative, held_out = negative_case("SELECT (", error_types=["syntax_error"]), canonical_case()
    assert isolation_errors([negative, held_out]) == []
    root, output = tmp_path / "gold", tmp_path / "export"
    write_cases(root, [negative, held_out])
    export_gold_set(root, output, EVALUATION_SCHEMA, SqlSafetyPolicy())
    assert verify_generation(output) == []
    assert any(
        "question leakage" in error
        for error in isolation_errors(
            [negative.model_copy(update={"question": held_out.question}), held_out]
        )
    )
    assert any(
        "family leakage" in error
        for error in isolation_errors(
            [negative.model_copy(update={"query_family_id": held_out.query_family_id}), held_out]
        )
    )


@pytest.mark.parametrize(
    ("sql", "error_types"),
    [
        ("SELECT (", ["semantic_error"]),
        ("DELETE FROM Fact", ["syntax_error"]),
        ("SELECT COUNT(*) FROM Fact; SELECT (", ["syntax_error"]),
        ("DELETE FROM Fact; SELECT (", ["syntax_error"]),
        ("SELECT 'unterminated", ["syntax_error"]),
    ],
)
def test_only_confirmed_and_declared_syntax_errors_are_exempt(sql, error_types):
    assert isolation_errors([negative_case(sql, error_types=error_types)])


@pytest.mark.parametrize(
    "suffix", [")", "+", "WHERE (", "WHERE", "ORDER BY", "GROUP BY", "UNION", "UNION SELECT ("]
)
def test_identifiable_test_body_cannot_leak_through_a_broken_syntax_suffix(tmp_path, suffix):
    held_out = canonical_case()
    negative = negative_case(
        held_out.payload["baseline_sql"] + " " + suffix, error_types=["syntax_error"]
    )
    assert any("cannot safely isolate" in error for error in isolation_errors([negative, held_out]))
    root, output = tmp_path / "gold", tmp_path / "export"
    write_cases(root, [negative, held_out])
    with pytest.raises(ValueError, match="cannot safely isolate"):
        export_gold_set(root, output, EVALUATION_SCHEMA, SqlSafetyPolicy())
    assert not output.exists()


def test_wrong_schema_or_unauthorized_sql_cannot_be_exported():
    case = canonical_case()
    assert validate_approved_cases([case], EVALUATION_SCHEMA, SqlSafetyPolicy()) == []
    assert validate_approved_cases(
        [case], EVALUATION_SCHEMA, SqlSafetyPolicy(denied_tables=("Fact",))
    )
    wrong = case.model_copy(
        update={"review": case.review.model_copy(update={"schema_fingerprint": "c" * 64})}
    )
    assert validate_approved_cases([wrong], EVALUATION_SCHEMA, SqlSafetyPolicy())


def test_real_repository_migration_preserves_bytes_and_keeps_every_case_pending():
    root = Path(__file__).resolve().parents[1]
    canonical = root / "evaluation/gold_set"
    manifest = json.loads((canonical / "manifest.json").read_text(encoding="utf-8"))
    cases = load_gold_cases(canonical)
    assert len(cases) >= manifest["audit"]["total"] == 38
    assert manifest["audit"]["states"] == {"candidate": 38}
    for relative, digest in manifest["sources"].items():
        assert sha256((canonical / "legacy" / relative).read_bytes()).hexdigest() == digest
    assert any(
        "sql_template leakage" in error for error in manifest["audit"]["draft_isolation_findings"]
    )
    for split in ("dev", "test", "retrieval_train", "retrieval_calibration", "retrieval_test"):
        assert not (root / "evaluation" / f"{split}.jsonl").read_bytes()


def test_migration_is_non_destructive_and_never_inherits_approval(tmp_path):
    from text2sql.evaluation.gold_set import EXPORT_PATHS

    workspace = tmp_path / "workspace"
    content = (
        json.dumps({"id": "legacy", "question": "legacy question", "status": "approved"}).encode()
        + b"\n"
    )
    for relative in [*EXPORT_PATHS.values(), "knowledge/examples/migration_review.jsonl"]:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    canonical = tmp_path / "canonical"
    migrate_legacy_sources(workspace, canonical)
    assert (workspace / "evaluation/test.jsonl").read_bytes() == content
    assert {case.status for case in load_gold_cases(canonical)} == {"candidate"}
