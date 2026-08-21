import importlib.util
from pathlib import Path
import sys
import unittest


def load_tests(loader: unittest.TestLoader, tests, pattern):
    suite = unittest.TestSuite()
    runtime_tests_dir = Path(__file__).resolve().parent / "agent_runtime"
    for path in sorted(runtime_tests_dir.glob(pattern or "test*.py")):
        module_name = f"_helperme_agent_runtime_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load test module: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite
