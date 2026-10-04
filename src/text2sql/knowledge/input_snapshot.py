"""Read-once training inputs with immutable bytes and pre-publication checks."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any


class InputSnapshotError(ValueError):
    """Training inputs are unavailable or changed before publication."""


class InputSnapshot:
    """Parse and hash the same captured bytes, including during an ABA change.

    Logical absolute paths are stable even if a symlink's target is replaced.
    No reads after capture can consume replacement bytes. Publication checks
    detect current file/target, directory membership, and observed identity drift.
    """

    def __init__(self) -> None:
        self._base = Path.cwd()
        self._contents: dict[Path, bytes | None] = {}
        self._targets: dict[Path, Path] = {}
        self._directories: dict[Path, tuple[bool, tuple[str, ...]]] = {}
        self._identities: dict[str, tuple[Any, Callable[[], Any]]] = {}

    def _path(self, path: str | Path) -> Path:
        value = Path(path)
        return Path(os.path.abspath(value if value.is_absolute() else self._base / value))

    def read(self, path: str | Path) -> bytes | None:
        value = self._path(path)
        if value not in self._contents:
            self._targets[value] = value.resolve()
            try:
                self._contents[value] = value.read_bytes()
            except FileNotFoundError:
                self._contents[value] = None
            except OSError as exc:
                raise InputSnapshotError(f"training input cannot be read: {value}") from exc
        return self._contents[value]

    def require_read(self, path: str | Path) -> bytes:
        content = self.read(path)
        if content is None:
            raise InputSnapshotError(f"training input is missing: {self._path(path)}")
        return content

    def sha256(self, path: str | Path) -> str:
        content = self.read(path)
        return hashlib.sha256(content).hexdigest() if content is not None else "missing"

    @staticmethod
    def _members(root: Path) -> tuple[str, ...]:
        if not root.exists():
            return ()
        return tuple(
            sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
        )

    def pin_directory(self, root: str | Path) -> tuple[Path, ...]:
        value = self._path(root)
        if value not in self._directories:
            self._directories[value] = value.is_dir(), self._members(value)
            self._targets[value] = value.resolve()
        paths = tuple(value / relative for relative in self._directories[value][1])
        for path in paths:
            self.require_read(path)
        return paths

    def pin_identity(self, name: str, value: Any, observer: Callable[[], Any]) -> None:
        if name in self._identities:
            raise ValueError(f"training identity is already pinned: {name}")
        self._identities[name] = deepcopy(value), observer

    def changed_paths(self) -> list[str]:
        changed: set[str] = set()
        for path, expected in self._contents.items():
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                current = None
            except OSError:
                changed.add(str(path))
                continue
            if current != expected or path.resolve() != self._targets[path]:
                changed.add(str(path))
        for root, (exists, members) in self._directories.items():
            try:
                if (
                    root.is_dir() != exists
                    or self._members(root) != members
                    or root.resolve() != self._targets[root]
                ):
                    changed.add(str(root))
            except OSError:
                changed.add(str(root))
        for name, (expected, observer) in self._identities.items():
            try:
                actual = observer()
            except Exception:
                changed.add(f"identity:{name}")
            else:
                if actual != expected:
                    changed.add(f"identity:{name}")
        return sorted(changed)

    def assert_unchanged(self) -> None:
        changed = self.changed_paths()
        if changed:
            raise InputSnapshotError(
                "training inputs changed before publication: " + "; ".join(changed)
            )
