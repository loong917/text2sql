from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
FORMAL_DOCS = [
    ROOT / "README.MD",
    ROOT / "ARCHITECTURE.md",
    ROOT / "knowledge" / "README.md",
    *(ROOT / "docs").glob("*.md"),
]


class DocumentationTests(unittest.TestCase):
    def test_document_structure_has_no_obsolete_root_files(self):
        self.assertFalse((ROOT / "KNOWLEDGE.md").exists())
        self.assertFalse((ROOT / "RETRIEVAL.md").exists())
        self.assertFalse((ROOT / "OPTIMIZATION.md").exists())
        self.assertTrue((ROOT / "knowledge" / "README.md").exists())
        self.assertTrue((ROOT / "docs" / "RETRIEVAL.md").exists())
        self.assertTrue((ROOT / "docs" / "OPERATIONS.md").exists())

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
