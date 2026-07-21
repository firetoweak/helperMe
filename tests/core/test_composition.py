import tempfile
import unittest
from pathlib import Path

from core.composition import create_agent_application
from core.runtime_modes import PlainMode, RunMode, RuntimeModeRouter
from core.todos import TodoMode


class CompositionTest(unittest.TestCase):
    def test_runtime_router_is_the_default_runtime_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                runtime_root=Path(directory),
            )

        runtime = application._session_runtime.run_runtime
        self.assertIsNone(runtime.runtime_mode)
        self.assertIsInstance(runtime.mode_router, RuntimeModeRouter)
        self.assertIsInstance(runtime.runtime_modes[RunMode.PLAIN], PlainMode)
        self.assertIsInstance(runtime.runtime_modes[RunMode.TODO], TodoMode)

    def test_explicit_runtime_mode_overrides_default(self):
        mode = PlainMode()
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                runtime_root=Path(directory),
                runtime_mode=mode,
            )

        self.assertIs(
            application._session_runtime.run_runtime.runtime_mode,
            mode,
        )


if __name__ == "__main__":
    unittest.main()
