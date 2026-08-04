import tempfile
import unittest
import json
from pathlib import Path

from core.composition import create_agent_application
from core.runtime_artifacts import ArtifactNotFoundError
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
            application.create_session("session-1")

        runtime = application._session_runtime._session_run_runtimes[
            "session-1"
        ]
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
            application.create_session("session-1")

        self.assertIs(
            application._session_runtime._session_run_runtimes[
                "session-1"
            ].runtime_mode,
            mode,
        )

    def test_each_session_gets_a_private_run_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                runtime_root=Path(directory),
            )
            application.create_session("session-a")
            application.create_session("session-b")

        runtimes = application._session_runtime._session_run_runtimes
        self.assertIsNot(runtimes["session-a"], runtimes["session-b"])
        self.assertIsNot(
            runtimes["session-a"].tool_result_externalizer.store,
            runtimes["session-b"].tool_result_externalizer.store,
        )

    def test_read_artifact_is_bound_to_current_session_drawer(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                runtime_root=Path(directory),
            )
            application.create_session("session-a")
            application.create_session("session-b")
            runtimes = application._session_runtime._session_run_runtimes
            ref = runtimes["session-a"].tool_result_externalizer.store.save(
                "A content"
            )

            own_result = runtimes["session-a"].tools_executor.execute(
                "read_artifact",
                json.dumps({"artifact_id": ref.artifact_id}),
            )
            foreign_result = runtimes["session-b"].tools_executor.execute(
                "read_artifact",
                json.dumps({"artifact_id": ref.artifact_id}),
            )

        self.assertEqual(own_result["data"]["content"], "A content")
        self.assertEqual(foreign_result["code"], "ARTIFACT_NOT_FOUND")

    def test_delete_session_removes_its_artifact_drawer(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                runtime_root=Path(directory),
            )
            application.create_session("session-1")
            runtime = application._session_runtime._session_run_runtimes[
                "session-1"
            ]
            store = runtime.tool_result_externalizer.store
            ref = store.save("persistent content")

            application.delete_session("session-1")

            self.assertNotIn(
                "session-1",
                application._session_runtime.sessions,
            )
            with self.assertRaises(ArtifactNotFoundError):
                store.read(ref.artifact_id, 0, 100)


if __name__ == "__main__":
    unittest.main()
