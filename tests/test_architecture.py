import ast
from pathlib import Path
import unittest


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
            "application": {"api", "training"},
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

    def test_canonical_modules_do_not_import_legacy_paths(self):
        violations = []
        for layer in ("domain", "infrastructure", "application", "api", "training"):
            for path in (SRC / layer).glob("*.py"):
                text = path.read_text(encoding="utf-8")
                if "services." in text or "core.agent" in text:
                    violations.append(str(path.relative_to(SRC)))
        self.assertEqual(violations, [])
