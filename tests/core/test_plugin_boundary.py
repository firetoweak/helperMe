import ast
from pathlib import Path
import unittest


class PluginBoundaryTest(unittest.TestCase):
    def test_core_does_not_import_plugins(self):
        core_root = Path(__file__).resolve().parents[2] / "core"
        violations = []

        for path in core_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported_modules = [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            ]
            imported_modules.extend(
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            )
            if any(
                module == "plugins" or module.startswith("plugins.")
                for module in imported_modules
            ):
                violations.append(path.relative_to(core_root).as_posix())

        self.assertEqual(violations, [])
