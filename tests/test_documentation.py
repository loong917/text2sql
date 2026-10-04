import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORMAL_DOCS = [
    ROOT / "README.MD",
    ROOT / "ARCHITECTURE.md",
    *(ROOT / "docs").glob("*.md"),
    *(
        path
        for directory in ("src", "knowledge", "evaluation", "tests", ".github")
        for path in (ROOT / directory).rglob("README.md")
    ),
]


class DocumentationTests(unittest.TestCase):
    def test_maintained_packages_have_reusable_readme_assets(self):
        directories = [
            ROOT / name for name in ("src", "docs", "knowledge", "evaluation", "tests", ".github")
        ]
        directories.extend((ROOT / "src" / "text2sql").glob("*/__init__.py"))
        for directory in directories:
            path = (directory.parent if directory.is_file() else directory) / "README.md"
            self.assertTrue(path.is_file(), str(path.relative_to(ROOT)))
            content = path.read_text(encoding="utf-8")
            self.assertIn("## ", content)
            self.assertTrue(any(term in content for term in ("验收", "门禁")), str(path))

    def test_online_flow_contains_actual_gates_modes_and_bounded_retry(self):
        content = (ROOT / "README.MD").read_text(encoding="utf-8")
        blocks = re.findall(r"(?:```|~~~)mermaid\n(.*?)(?:```|~~~)", content, re.S)
        self.assertEqual(len(blocks), 1)
        flow = blocks[0]
        self.assertTrue(flow.startswith("flowchart TD"))
        for node in (
            "authGate",
            "releaseGate",
            "schemaCheck",
            "semanticPlan",
            "planReady",
            "retrieval",
            "contextReady",
            "compiler",
            "modelGenerate",
            "astValidation",
            "sqlValid",
            "retryPolicy",
            "executionMode",
            "sqlOnly",
            "boundedQuery",
            "candidateFeedback",
            "reviewedRelease",
        ):
            self.assertRegex(flow, rf"\b{node}[\[{{]")
        self.assertIn('executionMode -->|"否"| sqlOnly', flow)
        self.assertIn('executionMode -->|"是"| boundedQuery', flow)
        self.assertIn('retryPolicy -->|"允许模型重试"| modelGenerate', flow)
        self.assertNotIn("<br", flow)
        source = ROOT / "src" / "text2sql"
        service = (source / "application" / "text2sql_service.py").read_text(encoding="utf-8")
        self.assertIn("self._deps.compiler.compile", service)
        self.assertIn("self._deps.validator.validate", service)
        self.assertIn("if not execute_sql", service)
        self.assertIn("execute_query", service)

    def test_flow_source_mapping_paths_resolve(self):
        content = (ROOT / "docs" / "TEXT2SQL_FLOW.md").read_text(encoding="utf-8")
        for relative in re.findall(
            r"\b(?:api|application|domain|infrastructure|retrieval|release)/[a-z_]+\.py", content
        ):
            self.assertTrue((ROOT / "src" / "text2sql" / relative).is_file(), relative)

    def test_ci_has_readonly_pinned_actions_and_preserved_distribution_evidence(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        actions = re.findall(r"uses: (\S+)", workflow)
        self.assertTrue(actions)
        self.assertTrue(
            all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", action) for action in actions)
        )
        self.assertIn("contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("node --check src/text2sql/static/index.js", workflow)
        self.assertIn("SHA256SUMS", workflow)
        self.assertIn("if-no-files-found: error", workflow)
        self.assertIn("pip-audit==2.10.1", workflow)
        self.assertIn("dependency-audit.json", workflow)
        self.assertNotIn("--ignore-vuln", workflow)
        self.assertNotIn("continue-on-error", workflow)

    def test_env_example_covers_all_runtime_environment_keys(self):
        config_path = ROOT / "src" / "text2sql" / "core" / "config.py"
        tree = ast.parse(config_path.read_text(encoding="utf-8"))
        runtime_keys = {
            str(node.args[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr == "getenv"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        template_keys = {
            match.group(1)
            for match in re.finditer(
                r"(?m)^([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(encoding="utf-8")
            )
        }
        self.assertEqual(runtime_keys, template_keys)

    def test_document_structure_has_no_obsolete_root_files(self):
        self.assertFalse((ROOT / "KNOWLEDGE.md").exists())
        self.assertFalse((ROOT / "RETRIEVAL.md").exists())
        self.assertFalse((ROOT / "OPTIMIZATION.md").exists())
        self.assertTrue((ROOT / "knowledge" / "README.md").exists())
        self.assertTrue((ROOT / "docs" / "RETRIEVAL.md").exists())
        self.assertTrue((ROOT / "docs" / "OPERATIONS.md").exists())
        self.assertFalse((ROOT / "requirements.txt").exists())

    def test_documentation_has_no_removed_migration_command(self):
        references = [
            str(path.relative_to(ROOT))
            for path in FORMAL_DOCS
            if "text2sql-migrate-knowledge" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(references, [])

    def test_upgrade_assets_explain_negative_isolation_and_old_collection_evidence(self):
        gold = (ROOT / "docs" / "GOLD_SET.md").read_text(encoding="utf-8")
        self.assertIn("negative", gold)
        self.assertIn("syntax_error", gold)
        self.assertIn("真实解析失败", gold)
        self.assertIn("集合级检索配置", gold)
        self.assertIn("旧 v1", gold)
        upgrade = (ROOT / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
        self.assertIn("TRAINING_SKIP_UNCHANGED=false", upgrade)
        self.assertIn("38 candidate / 0 approved", upgrade)
        flow = (ROOT / "docs" / "TEXT2SQL_FLOW.md").read_text(encoding="utf-8")
        self.assertIn('approved --> isolation["正例与可解析反例模板隔离"]', flow)
        self.assertIn("集合内容与配置证据 v2", flow)

    def test_relative_markdown_links_resolve(self):
        broken = []
        for document in FORMAL_DOCS:
            content = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", content):
                if "://" in target or target.startswith("#"):
                    continue
                relative = target.split("#", 1)[0]
                if not (document.parent / relative).resolve().exists():
                    broken.append(f"{document.relative_to(ROOT)} -> {target}")
        self.assertEqual(broken, [])
