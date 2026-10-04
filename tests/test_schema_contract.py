import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from text2sql.knowledge.provenance import schema_fingerprint
from text2sql.knowledge.schema_contract import (
    SchemaContractError,
    require_schema_contract,
    usable_foreign_keys,
    usable_single_column_foreign_keys,
    validate_schema_contract,
)
from text2sql.retrieval.schema_graph import shortest_path
from text2sql.retrieval.table_card import build_table_cards, load_schema_snapshot


def valid_schema():
    return {
        "Fact": {
            "schema_name": "dbo",
            "qualified_name": "dbo.Fact",
            "object_id": 1,
            "columns": {
                "DimID": {"data_type": "int", "is_nullable": False},
                "Amount": {
                    "data_type": "decimal",
                    "is_nullable": True,
                    "precision": 18,
                    "scale": 2,
                },
            },
            "primary_key": [],
            "unique_keys": {},
            "foreign_keys": [
                {
                    "column_name": "DimID",
                    "referenced_table": "audit.Dimension",
                    "referenced_column": "ID",
                    "constraint_name": "FK_dim",
                    "ordinal": 1,
                    "is_disabled": False,
                    "is_not_trusted": False,
                }
            ],
        },
        "audit.Dimension": {
            "schema_name": "audit",
            "qualified_name": "audit.Dimension",
            "object_id": 2,
            "columns": {
                "ID": {
                    "data_type": "int",
                    "is_nullable": False,
                    "is_identity": True,
                    "is_computed": False,
                    "max_length": 4,
                }
            },
            "primary_key": ["ID"],
            "unique_keys": {"PK_dim": ["ID"]},
            "foreign_keys": [],
        },
    }


def test_valid_contract_does_not_mutate_or_default_metadata():
    schema = valid_schema()
    before = deepcopy(schema)
    assert validate_schema_contract(schema) == []
    assert require_schema_contract(schema) is schema
    assert schema == before


@pytest.mark.parametrize("schema", [None, [], {}, {"Fact": None}, {"Fact": {}}])
def test_incomplete_schema_fails_closed(schema):
    assert validate_schema_contract(schema)
    with pytest.raises(SchemaContractError, match="invalid authoritative Schema"):
        require_schema_contract(schema)


@pytest.mark.parametrize("value", [None, "", " ", 123, " int "])
def test_column_type_must_be_a_real_nonempty_string(value):
    schema = valid_schema()
    schema["Fact"]["columns"]["DimID"]["data_type"] = value
    assert any("data_type" in error for error in validate_schema_contract(schema))


@pytest.mark.parametrize("value", [None, "False", "true", 0, 1, 2])
def test_nullable_must_be_an_exact_bool(value):
    schema = valid_schema()
    schema["Fact"]["columns"]["DimID"]["is_nullable"] = value
    assert any("is_nullable" in error for error in validate_schema_contract(schema))


@pytest.mark.parametrize("field", ["primary_key", "unique_keys", "foreign_keys"])
def test_constraint_absence_requires_explicit_empty_metadata(field):
    schema = valid_schema()
    del schema["audit.Dimension"][field]
    assert any(field in error for error in validate_schema_contract(schema))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_name", ""),
        ("schema_name", "other"),
        ("qualified_name", "Dimension"),
        ("qualified_name", "other.Dimension"),
        ("object_id", None),
        ("object_id", True),
    ],
)
def test_table_identity_is_not_guessed(field, value):
    schema = valid_schema()
    schema["audit.Dimension"][field] = value
    assert validate_schema_contract(schema)


def test_ambiguous_table_or_column_case_and_object_ids_are_rejected():
    schema = valid_schema()
    schema["fact"] = deepcopy(schema["Fact"])
    schema["Fact"]["columns"]["dimid"] = {"data_type": "int", "is_nullable": False}
    errors = validate_schema_contract(schema)
    assert any("duplicate table" in error for error in errors)
    assert any("duplicate column" in error for error in errors)
    assert any("duplicate physical" in error for error in errors)


@pytest.mark.parametrize(
    "field,value",
    [
        ("primary_key", ["missing"]),
        ("primary_key", ["ID", "ID"]),
        ("primary_key", "ID"),
        ("unique_keys", {"PK_dim": []}),
        ("unique_keys", {"PK_dim": ["missing"]}),
        ("unique_keys", {"PK_dim": 1}),
    ],
)
def test_primary_and_unique_keys_reference_actual_columns(field, value):
    schema = valid_schema()
    schema["audit.Dimension"][field] = value
    assert validate_schema_contract(schema)


def test_primary_keys_cannot_be_nullable_or_missing_from_unique_keys():
    schema = valid_schema()
    schema["audit.Dimension"]["columns"]["ID"]["is_nullable"] = True
    schema["audit.Dimension"]["unique_keys"] = {}
    errors = validate_schema_contract(schema)
    assert any("cannot be nullable" in error for error in errors)
    assert any("must match" in error for error in errors)


@pytest.mark.parametrize(
    "field,value",
    [
        ("column_name", "missing"),
        ("referenced_table", "missing"),
        ("referenced_column", "missing"),
        ("constraint_name", ""),
        ("ordinal", 0),
        ("ordinal", True),
        ("is_disabled", None),
        ("is_not_trusted", "False"),
        ("referenced_column", ["ID"]),
    ],
)
def test_foreign_keys_must_have_complete_consistent_physical_metadata(field, value):
    schema = valid_schema()
    schema["Fact"]["foreign_keys"][0][field] = value
    assert validate_schema_contract(schema)


def test_composite_foreign_keys_need_all_key_columns_and_contiguous_ordinals():
    schema = valid_schema()
    dimension = schema["audit.Dimension"]
    dimension["columns"]["Code"] = {"data_type": "int", "is_nullable": False}
    dimension["primary_key"] = ["ID", "Code"]
    dimension["unique_keys"]["PK_dim"] = ["ID", "Code"]
    assert any("unique key" in error for error in validate_schema_contract(schema))
    schema["Fact"]["columns"]["DimCode"] = {"data_type": "int", "is_nullable": False}
    schema["Fact"]["foreign_keys"].append(
        {
            **schema["Fact"]["foreign_keys"][0],
            "column_name": "DimCode",
            "referenced_column": "Code",
            "ordinal": 2,
        }
    )
    assert validate_schema_contract(schema) == []
    schema["Fact"]["foreign_keys"][1]["ordinal"] = 1
    assert any("ordinals" in error for error in validate_schema_contract(schema))


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_length", -2),
        ("max_length", True),
        ("precision", 256),
        ("scale", "2"),
        ("is_identity", "false"),
        ("is_computed", 0),
        ("collation_name", ""),
    ],
)
def test_optional_column_details_are_strict_when_present(field, value):
    schema = valid_schema()
    schema["Fact"]["columns"]["DimID"][field] = value
    assert validate_schema_contract(schema)


def test_decimal_metadata_and_impossible_identity_computed_combination():
    schema = valid_schema()
    schema["Fact"]["columns"]["Amount"].update(precision=2, scale=3)
    schema["audit.Dimension"]["columns"]["ID"]["is_computed"] = True
    errors = validate_schema_contract(schema)
    assert any("scale" in error for error in errors)
    assert any("both identity and computed" in error for error in errors)


@pytest.mark.parametrize("field", ["is_disabled", "is_not_trusted"])
def test_disabled_or_untrusted_fk_is_preserved_but_not_learned(field):
    schema = valid_schema()
    schema["Fact"]["foreign_keys"][0][field] = True
    assert validate_schema_contract(schema) == []
    assert list(usable_foreign_keys(schema["Fact"])) == []
    assert shortest_path(schema, "Fact", "audit.Dimension") == []
    card = next(card for card in build_table_cards(schema) if card.table_name == "Fact")
    assert "FK_dim" not in card.text
    assert "->" not in card.text


def test_missing_fk_trust_is_never_treated_as_trusted():
    assert list(usable_foreign_keys({"foreign_keys": [{"referenced_table": "Dimension"}]})) == []
    schema = valid_schema()
    del schema["Fact"]["foreign_keys"][0]["is_not_trusted"]
    with pytest.raises(SchemaContractError):
        build_table_cards(schema)


def test_graph_and_cards_include_valid_trusted_fk_and_metadata():
    schema = valid_schema()
    assert shortest_path(schema, "Fact", "audit.Dimension") == ["Fact", "audit.Dimension"]
    assert shortest_path(schema, "unknown", "unknown") == []
    card = next(card for card in build_table_cards(schema) if card.table_name == "Fact")
    assert "FK_dim: Fact.(DimID) -> audit.Dimension.(ID)" in card.text
    assert "可空=False" in card.text
    assert "精度=18" in card.text
    assert "小数位=2" in card.text


def test_correct_fingerprint_cannot_make_an_incomplete_legacy_snapshot_authoritative(tmp_path):
    legacy = {"Fact": {"columns": {"ID": {"data_type": ""}}, "foreign_keys": []}}
    path = tmp_path / "schema.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": f"sha256:{schema_fingerprint(legacy)}",
                "tables": legacy,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SchemaContractError, match="data_type"):
        load_schema_snapshot(path)


def test_complete_source_snapshot_keeps_dictionary_shape(tmp_path):
    schema = valid_schema()
    path = tmp_path / "schema.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": f"sha256:{schema_fingerprint(schema)}",
                "tables": schema,
            }
        ),
        encoding="utf-8",
    )
    assert load_schema_snapshot(path) == schema


def test_export_rechecks_contract_before_touching_output(tmp_path):
    from text2sql.cli.export_schema_snapshot import main

    output = tmp_path / "preserved.json"
    output.write_text("last known good", encoding="utf-8")
    with (
        patch("sys.argv", ["export", "--snapshot", "candidate.json", "--output", str(output)]),
        patch("text2sql.cli.export_schema_snapshot.load_settings"),
        patch("text2sql.cli.export_schema_snapshot.ArtifactSnapshot.load") as loader,
    ):
        loader.return_value.schema = {"Fact": {"columns": {"ID": {"data_type": ""}}}}
        with pytest.raises(SchemaContractError):
            main()
    assert output.read_text(encoding="utf-8") == "last known good"


def test_live_export_does_not_need_an_artifact_and_closes_all_resources(tmp_path):
    from text2sql.cli.export_schema_snapshot import main

    schema = valid_schema()
    output = tmp_path / "live.json"
    runtime = SimpleNamespace(sql_runner=Mock(), close=Mock())
    executor = SimpleNamespace(aclose=AsyncMock())
    with (
        patch("sys.argv", ["export", "--live", "--output", str(output)]),
        patch("text2sql.cli.export_schema_snapshot.load_settings"),
        patch("text2sql.cli.export_schema_snapshot.RuntimeResources", return_value=runtime),
        patch("text2sql.cli.export_schema_snapshot.VannaSqlExecutor", return_value=executor),
        patch(
            "text2sql.cli.export_schema_snapshot.get_live_schema",
            new=AsyncMock(return_value=schema),
        ) as load,
        patch("text2sql.cli.export_schema_snapshot.ArtifactSnapshot.load") as frozen,
        patch("text2sql.cli.export_schema_snapshot.KnowledgeArtifactRegistry") as registry,
    ):
        main()
    frozen.assert_not_called()
    registry.assert_not_called()
    executor.aclose.assert_awaited_once()
    runtime.close.assert_called_once()
    assert load.call_args.kwargs["force_refresh"] is True
    assert load.call_args.kwargs["state"].live_schema is None
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["tables"] == schema
    assert payload["source"] == "sql-server:sys-catalog"
    assert payload["schema_version"] == f"sha256:{schema_fingerprint(schema)}"


@pytest.mark.parametrize("failure", ["read", "construct", "contract", "close"])
def test_live_export_failure_preserves_previous_output_and_closes_runtime(tmp_path, failure):
    from text2sql.cli.export_schema_snapshot import main

    output = tmp_path / "live.json"
    output.write_text("last known good", encoding="utf-8")
    runtime = SimpleNamespace(sql_runner=Mock(), close=Mock())
    executor = SimpleNamespace(aclose=AsyncMock())
    if failure == "close":
        executor.aclose.side_effect = RuntimeError("worker close failed")
    loader = AsyncMock(return_value=valid_schema())
    if failure == "read":
        loader.side_effect = RuntimeError("metadata unavailable")
    if failure == "contract":
        loader.return_value = {"Fact": {"columns": {"ID": {}}}}
    with (
        patch("sys.argv", ["export", "--live", "--output", str(output)]),
        patch("text2sql.cli.export_schema_snapshot.load_settings"),
        patch("text2sql.cli.export_schema_snapshot.RuntimeResources", return_value=runtime),
        patch(
            "text2sql.cli.export_schema_snapshot.VannaSqlExecutor",
            return_value=executor,
            side_effect=RuntimeError("runner unavailable") if failure == "construct" else None,
        ),
        patch("text2sql.cli.export_schema_snapshot.get_live_schema", new=loader),
    ):
        with pytest.raises((RuntimeError, SchemaContractError)):
            main()
    runtime.close.assert_called_once()
    if failure != "construct":
        executor.aclose.assert_awaited_once()
    assert output.read_text(encoding="utf-8") == "last known good"


def test_live_export_and_artifact_source_are_mutually_exclusive():
    from text2sql.cli.export_schema_snapshot import main

    with patch("sys.argv", ["export", "--live", "--snapshot", "knowledge.json"]):
        with pytest.raises(SystemExit):
            main()


def test_composite_fk_is_described_completely_but_not_learned_as_single_join():
    schema = valid_schema()
    schema["Fact"]["columns"]["Tenant"] = {"data_type": "int", "is_nullable": False}
    schema["audit.Dimension"]["columns"]["Tenant"] = {"data_type": "int", "is_nullable": False}
    schema["audit.Dimension"]["primary_key"] = ["ID", "Tenant"]
    schema["audit.Dimension"]["unique_keys"] = {"PK_dim": ["ID", "Tenant"]}
    second = schema["Fact"]["foreign_keys"][0] | {
        "column_name": "Tenant",
        "referenced_column": "Tenant",
        "ordinal": 2,
    }
    schema["Fact"]["foreign_keys"].append(second)
    require_schema_contract(schema)
    assert list(usable_single_column_foreign_keys(schema["Fact"])) == []
    assert shortest_path(schema, "Fact", "audit.Dimension") == []
    card = next(card for card in build_table_cards(schema) if card.table_name == "Fact")
    assert "(DimID,Tenant)" in card.text.replace(" ", "")
