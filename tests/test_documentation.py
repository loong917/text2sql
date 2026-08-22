import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORMAL_DOCS = [
    ROOT / "README.MD",
    ROOT / "ARCHITECTURE.md",
    ROOT / "knowledge" / "README.md",
    *(ROOT / "docs").glob("*.md"),
]


class DocumentationTests(unittest.TestCase):
    def test_env_example_covers_all_runtime_environment_keys(self):
        config_path = ROOT / "src" / "core" / "config.py"
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
