"""Smoke-test an installed wheel away from the checkout when a build is provided."""

import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from text2sql.core.build_info import build_metadata, code_fingerprint


def test_container_build_backend_is_pinned_and_hash_verified():
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = (root / "build-requirements.lock").read_text(encoding="utf-8")
    for requirement in config["build-system"]["requires"]:
        assert "==" in requirement
        assert requirement in lock
    assert "--hash=sha256:" in lock
    docker = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --require-hashes --no-deps -r build-requirements.lock" in docker
    assert "pip install --no-build-isolation --no-deps ." in docker


def test_installed_wheel_has_resources_and_portable_identity(tmp_path):
    wheel_dir = os.environ.get("TEXT2SQL_WHEEL_DIR")
    if not wheel_dir:
        pytest.skip("set TEXT2SQL_WHEEL_DIR after building the wheel")
    wheels = list(Path(wheel_dir).resolve().glob("text2sql-*.whl"))
    assert len(wheels) == 1, "provide exactly one current wheel"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        metadata = json.loads(archive.read("text2sql/build-info.json"))
        assert (
            metadata["dependency_lock_fingerprints"]
            == build_metadata()["dependency_lock_fingerprints"]
        )
        assert "text2sql/build-info.json" in names
        assert "text2sql/templates/index.html" in names
        assert "text2sql/static/index.js" in names
        assert "text2sql/domain/semantic_contract.py" in names
        assert "text2sql/evaluation/semantic_snapshot.py" not in names
        assert not any(name.startswith(("src/", "scripts/")) for name in names)
    installed = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            "--target",
            str(installed),
            str(wheels[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(installed),
        "APP_ENV": "test",
        "APP_DATA_DIR": str(tmp_path / "data"),
        "MSSQL_CONN_STR": "Driver={Missing Test Driver};Server=localhost;Database=test;UID=test;PWD=test",
    }
    smoke = r"""
import json
from pathlib import Path
import text2sql
from fastapi.testclient import TestClient
from text2sql.api.server import create_app
from text2sql.core.build_info import build_metadata, code_fingerprint
from text2sql.core.config import load_settings
config = load_settings()
assert Path(text2sql.__file__).is_relative_to(Path.cwd() / "installed")
assert Path(config.structured_knowledge_dir).is_relative_to(Path.cwd() / "data")
metadata = build_metadata()
assert all(value != "missing" for value in metadata["dependency_lock_fingerprints"].values())
assert metadata["code_fingerprint"] == code_fingerprint()
with TestClient(create_app(config)) as client:
    assert client.get("/").status_code == 200
    assert client.get("/static/index.css").status_code == 200
    assert client.get("/livez").json() == {"status": "alive"}
    assert client.get("/readyz").status_code == 503
print(json.dumps({"code_fingerprint": code_fingerprint()}))
"""
    result = subprocess.run(
        [sys.executable, "-c", smoke],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        json.loads(result.stdout.strip().splitlines()[-1])["code_fingerprint"] == code_fingerprint()
    )
