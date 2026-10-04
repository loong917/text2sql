"""Embed source and dependency-lock identity in built distributions."""

import hashlib
import json
import re
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        root = Path(__file__).resolve().parent
        package = root / "src" / "text2sql"
        build_package = (Path(self.build_lib) / "text2sql").resolve()
        if build_package.is_dir():
            # setuptools copies new sources but does not remove old modules;
            # stale compatibility code must not leak from a dirty build cache.
            for path in build_package.rglob("*"):
                if not path.is_file() or path.suffix not in {".py", ".css", ".js", ".html"}:
                    continue
                if not path.resolve().is_relative_to(build_package):
                    raise RuntimeError("build cache contains an out-of-tree file")
                if not (package / path.relative_to(build_package)).is_file():
                    path.unlink()
        super().run()
        digest = hashlib.sha256()
        for path in sorted(package.rglob("*")):
            if not path.is_file() or (
                path.suffix not in {".py", ".css", ".js", ".html"} and path.name != "py.typed"
            ):
                continue
            digest.update(path.relative_to(package).as_posix().encode())
            digest.update(path.read_bytes())
        metadata = {
            "code_fingerprint": digest.hexdigest(),
            "runtime_requirements": [
                line.rstrip("\\").strip()
                for line in (root / "requirements.lock").read_text(encoding="utf-8").splitlines()
                if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^]]+\])?==", line)
            ],
            "dependency_lock_fingerprints": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in ("uv.lock", "requirements.lock", "build-requirements.lock")
            },
        }
        target = Path(self.build_lib) / "text2sql"
        target.mkdir(parents=True, exist_ok=True)
        (target / "build-info.json").write_text(
            json.dumps(metadata, sort_keys=True), encoding="utf-8"
        )


setup(cmdclass={"build_py": BuildPy})
