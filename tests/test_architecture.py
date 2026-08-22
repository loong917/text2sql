import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def imported_layers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    layers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if node.level == 2 and module:
            layers.add(module.split(".", 1)[0])
        elif module.startswith("src."):
            layers.add(module.split(".", 2)[1])
    return layers


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_layer_dependencies_point_inward(self):
        forbidden = {
            "domain": {"application", "infrastructure", "api", "training"},
            "infrastructure": {"application", "api", "training"},
            "application": {"api", "bootstrap", "infrastructure", "training"},
        }
        violations = []
        for layer, blocked in forbidden.items():
            for path in (SRC / layer).glob("*.py"):
                found = imported_layers(path) & blocked
                if found:
                    violations.append(f"{path.name}: {sorted(found)}")
        self.assertEqual(violations, [])

    def test_removed_legacy_layers_do_not_reappear(self):
        self.assertFalse((SRC / "services").exists())
        self.assertFalse((SRC / "core" / "agent.py").exists())
        self.assertFalse((SRC / "train.py").exists())
        self.assertFalse((SRC / "application" / "wiring.py").exists())
        self.assertFalse((SRC.parent / "requirements.txt").exists())
        self.assertFalse((SRC.parent / "scripts" / "migrate_markdown_knowledge.py").exists())
        self.assertNotIn(
            "sys.path.insert",
            (SRC.parent / "scripts" / "export_schema_snapshot.py").read_text(encoding="utf-8"),
        )
        self.assertNotIn("sys.path.insert", (SRC / "__main__.py").read_text(encoding="utf-8"))

    def test_online_use_case_does_not_read_global_settings(self):
        use_case_modules = [
            "ports.py",
            "query_config.py",
            "query_prompt.py",
            "query_response.py",
            "text2sql_service.py",
        ]
        violations = []
        for name in use_case_modules:
            source = (SRC / "application" / name).read_text(encoding="utf-8")
            if "core.config" in source or "settings." in source:
                violations.append(name)
        self.assertEqual(violations, [])

    def test_domain_has_no_embedded_business_catalog(self):
        source = (SRC / "domain" / "semantic_ir.py").read_text(encoding="utf-8")
        self.assertNotIn("CITY_ALIASES", source)
        self.assertNotIn("DEFAULT_CATALOG", source)

    def test_training_pipeline_delegates_evaluation(self):
        source = (SRC / "training" / "pipeline.py").read_text(encoding="utf-8")
        self.assertNotIn("def _evaluate_sql_case", source)
        self.assertNotIn("def _semantic_ir_projection", source)
        self.assertNotIn("def _empty_training_report", source)
        self.assertLess(len(source.splitlines()), 1100)

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
        for layer in ("domain", "infrastructure", "application", "api", "training"):
            for path in (SRC / layer).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                modules = [
                    node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                ]
                modules.extend(
                    alias.name
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Import)
                    for alias in node.names
                )
                if any(
                    module == "services"
                    or module.startswith("services.")
                    or module == "core.agent"
                    or module.startswith("core.agent.")
                    for module in modules
                ):
                    violations.append(str(path.relative_to(SRC)))
        self.assertEqual(violations, [])
