"""Recursive import boundaries, including aliases and ordinary imports."""

import ast
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src" / "text2sql"
PACKAGE = "text2sql"

# Offline composition roots may assemble adapters; online application code may not.
ALLOWED_LAYERS = {
    "cli": {"cli", "core", "knowledge"},
    "core": {"core"},
    "domain": {"domain", "core"},
    "knowledge": {"knowledge", "domain", "core"},
    "application": {"application", "domain", "knowledge", "core"},
    "infrastructure": {"infrastructure", "application", "domain", "knowledge", "core", "retrieval"},
    "retrieval": {"retrieval", "domain", "knowledge", "core", "evaluation"},
    "bootstrap": {
        "bootstrap",
        "application",
        "domain",
        "infrastructure",
        "knowledge",
        "core",
        "retrieval",
    },
    "evaluation": {
        "evaluation",
        "application",
        "bootstrap",
        "domain",
        "infrastructure",
        "knowledge",
        "core",
        "retrieval",
    },
    "training": {
        "training",
        "application",
        "domain",
        "infrastructure",
        "knowledge",
        "core",
        "retrieval",
        "evaluation",
    },
    "release": {
        "release",
        "application",
        "bootstrap",
        "domain",
        "infrastructure",
        "knowledge",
        "core",
        "retrieval",
        "evaluation",
    },
    "api": {
        "api",
        "application",
        "bootstrap",
        "domain",
        "infrastructure",
        "knowledge",
        "core",
        "retrieval",
        "release",
    },
}
INFRASTRUCTURE_APPLICATION_MODULES = {"ports", "contracts", "context_state"}
EXTERNAL_ADAPTER_PACKAGES = {
    "vanna",
    "ollama",
    "chromadb",
    "chroma",
    "pyodbc",
    "fastapi",
    "starlette",
}


def import_edges(source: str, package: str) -> list[tuple[str, int]]:
    """Resolve static imports without importing modules or invoking clients."""
    edges = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            edges.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = parts[: len(parts) - node.level + 1]
                if node.module:
                    base.extend(node.module.split("."))
                module = ".".join(base)
            else:
                module = node.module or ""
            edges.extend((f"{module}.{alias.name}".strip("."), node.lineno) for alias in node.names)
    return edges


def dependency_violations(source: str, package: str, layer: str, module: str) -> list[str]:
    violations = []
    for dependency, line in import_edges(source, package):
        parts = dependency.split(".")
        if parts[0] != PACKAGE:
            if layer in {"application", "domain"} and parts[0] in EXTERNAL_ADAPTER_PACKAGES:
                violations.append(f"{module}:{line}: external adapter {dependency}")
            continue
        if len(parts) < 2:
            continue
        target = parts[1]
        allowed = ALLOWED_LAYERS[layer]
        if layer == "cli" and module in {"cli.gold_set", "cli.export_schema_snapshot"}:
            allowed = allowed | {
                "evaluation",
                "retrieval",
                "domain",
                "application",
                "infrastructure",
            }
        if layer == "retrieval" and module == "retrieval.train":
            allowed = allowed | {"application", "infrastructure"}
        if target not in allowed:
            violations.append(f"{module}:{line}: forbidden layer {dependency}")
        elif layer == "infrastructure" and target == "application":
            if len(parts) < 3 or parts[2] not in INFRASTRUCTURE_APPLICATION_MODULES:
                violations.append(
                    f"{module}:{line}: non-contract application dependency {dependency}"
                )
    return violations


def python_modules():
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).with_suffix("")
        if relative.parts[0] not in ALLOWED_LAYERS:
            continue
        package = ".".join((PACKAGE, *relative.parts[:-1]))
        yield path, package, ".".join(relative.parts), relative.parts[0]


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_unsafe_dependency_service_and_legacy_surfaces_are_not_imported(self):
        blocked = ("vanna.legacy", "chromadb.server", "chromadb.api.fastapi", "chromadb.auth")
        violations = []
        for path, package, module, _ in python_modules():
            for dependency, line in import_edges(path.read_text(encoding="utf-8"), package):
                if dependency.startswith(blocked):
                    violations.append(f"{module}:{line}: {dependency}")
        self.assertEqual(violations, [])

    def test_layer_dependencies_point_inward_recursively(self):
        violations = []
        visited = set()
        for path, package, module, layer in python_modules():
            visited.add(layer)
            violations.extend(
                dependency_violations(path.read_text(encoding="utf-8"), package, layer, module)
            )
        self.assertEqual(visited, set(ALLOWED_LAYERS))
        self.assertEqual(violations, [])

    def test_every_source_package_has_an_explicit_layer_policy(self):
        packages = {
            path.relative_to(SRC).parts[0]
            for path in SRC.rglob("*.py")
            if len(path.relative_to(SRC).parts) > 1
        }
        self.assertEqual(packages, set(ALLOWED_LAYERS))

    def test_import_parser_covers_nested_relative_plain_and_aliased_imports(self):
        source = """import text2sql.infrastructure.runtime as client
from ...api import server as delivery
from .. import context_state
from ollama import AsyncClient
"""
        self.assertEqual(
            {name for name, _ in import_edges(source, "text2sql.application.nested")},
            {
                "text2sql.infrastructure.runtime",
                "text2sql.api.server",
                "text2sql.application.context_state",
                "ollama.AsyncClient",
            },
        )
        violations = dependency_violations(
            source, "text2sql.application.nested", "application", "application.nested.case"
        )
        self.assertEqual(len(violations), 3)
        self.assertTrue(any("ollama.AsyncClient" in item for item in violations))

    def test_infrastructure_may_depend_on_ports_not_use_cases(self):
        allowed = "from ..application.ports import Retriever\nfrom ..application.contracts import QueryContext"
        blocked = "import text2sql.application.text2sql_service"
        self.assertEqual(
            dependency_violations(
                allowed, "text2sql.infrastructure", "infrastructure", "infrastructure.adapter"
            ),
            [],
        )
        self.assertEqual(
            len(
                dependency_violations(
                    blocked, "text2sql.infrastructure", "infrastructure", "infrastructure.adapter"
                )
            ),
            1,
        )

    def test_removed_legacy_layers_do_not_reappear(self):
        for relative in ("services", "core/agent.py", "train.py", "application/wiring.py"):
            self.assertFalse((SRC / relative).exists(), relative)
        self.assertFalse((PROJECT_ROOT / "requirements.txt").exists())
        self.assertFalse((PROJECT_ROOT / "scripts" / "migrate_markdown_knowledge.py").exists())
        for path in (SRC / "cli" / "export_schema_snapshot.py", SRC / "__main__.py"):
            self.assertNotIn("sys.path.insert", path.read_text(encoding="utf-8"))

    def test_application_does_not_load_global_settings(self):
        violations = []
        for path, package, module, layer in python_modules():
            if layer != "application":
                continue
            source = path.read_text(encoding="utf-8")
            for dependency, line in import_edges(source, package):
                if dependency.endswith((".core.config.settings", ".core.config.load_settings")):
                    violations.append(f"{module}:{line}: {dependency}")
            for node in ast.walk(ast.parse(source)):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "settings"
                ):
                    violations.append(f"{module}:{node.lineno}: global settings access")
        self.assertEqual(violations, [])

    def test_domain_has_no_embedded_business_catalog(self):
        source = (SRC / "domain" / "semantic_ir.py").read_text(encoding="utf-8")
        self.assertNotIn("CITY_ALIASES", source)
        self.assertNotIn("DEFAULT_CATALOG", source)

    def test_training_pipeline_only_orchestrates_modules(self):
        source = (SRC / "training" / "pipeline.py").read_text(encoding="utf-8")
        definitions = {
            node.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertEqual(
            definitions, {"train_knowledge", "train_knowledge_async", "build_candidate"}
        )
        self.assertLess(len(source.splitlines()), 420)
        for name in ("records.py", "storage.py", "fingerprint.py", "knowledge_builder.py"):
            self.assertTrue((SRC / "training" / name).is_file())
        for path in (SRC / "training").rglob("*.py"):
            self.assertNotIn("TRAINING_PRIORITY", path.read_text(encoding="utf-8"))

    def test_api_delivery_delegates_contracts_and_readiness(self):
        source = (SRC / "api" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("class AskRequest", source)
        self.assertNotIn("def _build_training_report_summary", source)
        self.assertLess(len(source.splitlines()), 400)

    def test_removed_runtime_compatibility_settings_do_not_reappear(self):
        source = (SRC / "core" / "config.py").read_text(encoding="utf-8")
        for legacy_name in (
            "training_manifest_path",
            "training_report_path",
            "knowledge_collection",
            "KNOWLEDGE_COLLECTION",
        ):
            self.assertNotIn(legacy_name, source)

    def test_context_state_is_container_scoped(self):
        context_source = (SRC / "application" / "context_service.py").read_text(encoding="utf-8")
        state_source = (SRC / "application" / "context_state.py").read_text(encoding="utf-8")
        self.assertNotIn("_live_schema_cache", context_source)
        self.assertNotIn("global _table_retriever", context_source)
        self.assertIn("class ContextRuntimeState", state_source)
        self.assertLess(len(context_source.splitlines()), 300)

    def test_canonical_modules_do_not_import_legacy_paths(self):
        violations = []
        for path, package, module, _ in python_modules():
            for dependency, line in import_edges(path.read_text(encoding="utf-8"), package):
                normalized = dependency.removeprefix(f"{PACKAGE}.")
                if (
                    normalized == "services"
                    or normalized.startswith(("services.", "core.agent."))
                    or normalized == "core.agent"
                ):
                    violations.append(f"{module}:{line}: {dependency}")
        self.assertEqual(violations, [])
