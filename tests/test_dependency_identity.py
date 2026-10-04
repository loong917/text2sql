"""All production runtime dependencies must participate in portable identity."""

from dataclasses import replace
from unittest.mock import patch

from text2sql.core.build_info import (
    dependency_versions,
    release_identity,
    runtime_dependency_errors,
)
from text2sql.core.config import load_settings
from text2sql.core.production import production_configuration_errors


def inventory(*requirements):
    return {"runtime_requirements": list(requirements), "dependency_lock_fingerprints": {}}


def test_all_locked_runtime_dependencies_are_attested_and_markers_honored():
    declarations = inventory(
        "pandas==2.0.0", "pydantic==2.0.0", 'absent==1.0; python_version < "2"'
    )
    with (
        patch("text2sql.core.build_info.build_metadata", return_value=declarations),
        patch(
            "text2sql.core.build_info.importlib.metadata.version",
            side_effect=lambda name: {"pandas": "2.0.0", "pydantic": "2.0.0"}[name],
        ),
    ):
        assert dependency_versions() == {"pandas": "2.0.0", "pydantic": "2.0.0"}
        assert runtime_dependency_errors() == []


def test_version_drift_changes_release_identity_and_blocks_production():
    config = replace(load_settings(), app_env="production")
    with patch("text2sql.core.build_info.build_metadata", return_value=inventory("pandas==2.0.0")):
        with patch("text2sql.core.build_info.importlib.metadata.version", return_value="2.0.0"):
            before = release_identity(config)
        with patch("text2sql.core.build_info.importlib.metadata.version", return_value="2.1.0"):
            after = release_identity(config)
            assert (
                "runtime dependency pandas must match locked version 2.0.0"
                in production_configuration_errors(config)
            )
    assert before != after


def test_invalid_inventory_does_not_silently_fall_back_to_shortlist():
    for declaration in ({}, inventory("pandas>=2"), inventory("pandas==2", "pandas==2")):
        with patch("text2sql.core.build_info.build_metadata", return_value=declaration):
            assert runtime_dependency_errors()


def test_source_inventory_covers_result_and_contract_dependencies():
    versions = dependency_versions()
    assert {"pandas", "pydantic", "sqlalchemy", "httpx", "uvicorn", "starlette"}.issubset(versions)
    assert runtime_dependency_errors() == []


def test_python_runtime_is_part_of_evaluation_and_release_identity():
    config = load_settings()
    with patch("text2sql.core.build_info.platform.python_version", return_value="3.12.9"):
        first = release_identity(config)
    with patch("text2sql.core.build_info.platform.python_version", return_value="3.13.2"):
        second = release_identity(config)
    assert first != second
    assert first["python_runtime"]["version"] == "3.12.9"
