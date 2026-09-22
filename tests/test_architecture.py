"""Keep business and storage modules independent of the web transport."""

import ast
from pathlib import Path
import unittest


class ArchitectureTests(unittest.TestCase):
    def test_business_modules_do_not_import_the_web_layer(self):
        root = Path(__file__).resolve().parents[1] / "ksq"
        for domain in ("dashboard", "data", "order", "robot"):
            for path in (root / domain).glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        names = [node.module or ""]
                        if node.module == "ksq":
                            names.extend("ksq." + alias.name for alias in node.names)
                    else:
                        continue
                    for name in names:
                        with self.subTest(file=path.name, imported=name):
                            self.assertFalse(name == "ksq.web" or name.startswith(("ksq.web.", "fastapi", "starlette", "http.server")))


if __name__ == "__main__":
    unittest.main()
