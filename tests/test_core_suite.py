import importlib.util
from pathlib import Path
import unittest


def load_tests(loader: unittest.TestLoader, tests, pattern):
    suite = unittest.TestSuite()
    core_tests_dir = Path(__file__).resolve().parent / "core"
    for path in sorted(core_tests_dir.glob(pattern or "test*.py")):
        module_name = f"_helperme_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load test module: {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite
