import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from core.composition import create_agent_application
from core.runtime_artifacts import ArtifactNotFoundError
from core.runtime_modes import PlainMode, RunMode, RuntimeModeRouter
from core.todos import TodoMode
from tools.workspace import FilesystemAccessMode


class CompositionTest(unittest.TestCase):
    def test_scoped_access_does_not_discover_host_roots(self):
        with tempfile.TemporaryDirectory() as runtime_directory, patch(
            "core.composition.discover_host_filesystem_roots"
        ) as discover:
            create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                runtime_root=Path(runtime_directory),
                workspace_roots={"project": Path.cwd()},
            )

        discover.assert_not_called()

    def test_host_access_adds_discovered_roots_for_every_session(self):
        with tempfile.TemporaryDirectory() as runtime_directory, \
                tempfile.TemporaryDirectory() as project_directory, patch(
                    "core.composition.discover_host_filesystem_roots",
                    return_value={"drive_z": Path(runtime_directory)},
                ):
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                runtime_root=Path(runtime_directory),
                workspace_roots={"project": Path(project_directory)},
                filesystem_access_mode=FilesystemAccessMode.HOST,
            )
            application.create_session("session-1")
            runtime = application._session_runtime._session_run_runtimes[
                "session-1"
            ]

            result = runtime.tools_executor.execute(
                "get_workspace_info",
                "{}",
            )

        self.assertEqual(
            result["data"]["roots"],
            [{"name": "project"}, {"name": "drive_z"}],
        )

    def test_host_access_rejects_explicit_root_name_collision(self):
        with tempfile.TemporaryDirectory() as runtime_directory, \
                tempfile.TemporaryDirectory() as workspace_directory, patch(
                    "core.composition.discover_host_filesystem_roots",
                    return_value={"drive_d": Path(workspace_directory)},
                ):
            with self.assertRaisesRegex(ValueError, "名称冲突"):
                create_agent_application(
                    model="test-model",
                    model_context_limit=10_000,
                    runtime_root=Path(runtime_directory),
                    workspace_roots={
                        "drive_d": Path(workspace_directory),
                    },
                    filesystem_access_mode=FilesystemAccessMode.HOST,
                )

    def test_workspace_tools_are_bound_by_composition_root(self):
        with tempfile.TemporaryDirectory() as runtime_directory, tempfile.TemporaryDirectory() as workspace_directory:
            workspace_root = Path(workspace_directory)
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                runtime_root=Path(runtime_directory),
                workspace_roots={"project": workspace_root},
            )
            application.create_session("session-1")
            runtime = application._session_runtime._session_run_runtimes["session-1"]

            missing_root = runtime.tools_executor.execute(
                "read_file",
                '{"root":"missing","path":"a.txt"}',
            )
            absolute_path = runtime.tools_executor.execute(
                "read_file",
                json.dumps({"root": "project", "path": str(workspace_root / "a.txt")}),
            )
            write_result = runtime.tools_executor.execute(
                "write_file",
                '{"root":"project","path":"docs/a.txt","content":"hello"}',
            )
            read_result = runtime.tools_executor.execute(
                "read_file",
                '{"root":"project","path":"docs/a.txt"}',
            )
            command_spec = runtime.tools_executor.registry.get("execute_command")

        self.assertEqual(missing_root["code"], "UNKNOWN_WORKSPACE_ROOT")
        self.assertEqual(absolute_path["code"], "ABSOLUTE_PATH_NOT_ALLOWED")
        self.assertEqual(write_result["code"], "FILE_CREATED")
        self.assertEqual(read_result["data"]["content"], "hello")
        self.assertIsNotNone(command_spec)

    def test_runtime_router_is_the_default_runtime_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                runtime_root=Path(directory),
                workspace_roots={"project": Path.cwd()},
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
                workspace_roots={"project": Path.cwd()},
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
                workspace_roots={"project": Path.cwd()},
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
                workspace_roots={"project": Path.cwd()},
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
                workspace_roots={"project": Path.cwd()},
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
