import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core.agent_workspace import AgentWorkspace
from core.composition import create_agent_application
from core.runtime_artifacts import ArtifactNotFoundError
from core.runtime_modes import PlainMode, RunMode, RuntimeModeRouter
from core.todos import TodoMode
from tools.workspace import FilesystemAccessMode


class CompositionTest(unittest.IsolatedAsyncioTestCase):
    async def start_application(self, application):
        await application.__aenter__()
        self.addAsyncCleanup(application.close)
        return application

    async def test_scoped_access_does_not_discover_host_roots(self):
        with tempfile.TemporaryDirectory() as runtime_directory, patch(
            "core.composition.discover_host_filesystem_roots"
        ) as discover:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(runtime_directory)),
                workspace_roots={"project": Path.cwd()},
            )
            await self.start_application(application)

        discover.assert_not_called()

    async def test_internally_created_llm_client_is_application_owned(self):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        with tempfile.TemporaryDirectory() as directory, patch(
            "core.composition.LLMClient",
            return_value=client,
        ):
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(directory)),
                workspace_roots={"project": Path.cwd()},
            )
            async with application:
                pass

        client.__aenter__.assert_awaited_once()
        client.__aexit__.assert_awaited_once()

    async def test_injected_llm_client_remains_caller_owned(self):
        client = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(directory)),
                workspace_roots={"project": Path.cwd()},
                llm_client=client,
            )
            async with application:
                pass

        client.__aenter__.assert_not_awaited()
        client.__aexit__.assert_not_awaited()

    async def test_host_access_adds_discovered_roots_for_every_session(self):
        with tempfile.TemporaryDirectory() as runtime_directory, \
                tempfile.TemporaryDirectory() as project_directory, patch(
                    "core.composition.discover_host_filesystem_roots",
                    return_value={"drive_z": Path(runtime_directory)},
                ):
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(runtime_directory)),
                workspace_roots={"project": Path(project_directory)},
                filesystem_access_mode=FilesystemAccessMode.HOST,
            )
            await self.start_application(application)
            application.create_session("session-1")
            runtime = application._session_runtime._session_run_runtimes[
                "session-1"
            ]

            result = await runtime.tools_executor.execute(
                "get_workspace_info",
                "{}",
            )

        self.assertEqual(
            result["data"]["roots"],
            [{"name": "project"}, {"name": "drive_z"}],
        )

    async def test_host_access_rejects_explicit_root_name_collision(self):
        with tempfile.TemporaryDirectory() as runtime_directory, \
                tempfile.TemporaryDirectory() as workspace_directory, patch(
                    "core.composition.discover_host_filesystem_roots",
                    return_value={"drive_d": Path(workspace_directory)},
                ):
            with self.assertRaisesRegex(ValueError, "名称冲突"):
                create_agent_application(
                    model="test-model",
                    model_context_limit=10_000,
                    agent_workspace=AgentWorkspace(Path(runtime_directory)),
                    workspace_roots={
                        "drive_d": Path(workspace_directory),
                    },
                    filesystem_access_mode=FilesystemAccessMode.HOST,
                )

    async def test_workspace_tools_are_bound_by_composition_root(self):
        with tempfile.TemporaryDirectory() as runtime_directory, tempfile.TemporaryDirectory() as workspace_directory:
            workspace_root = Path(workspace_directory)
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(runtime_directory)),
                workspace_roots={"project": workspace_root},
            )
            await self.start_application(application)
            application.create_session("session-1")
            runtime = application._session_runtime._session_run_runtimes["session-1"]

            missing_root = await runtime.tools_executor.execute(
                "read_file",
                '{"root":"missing","path":"a.txt"}',
            )
            absolute_path = await runtime.tools_executor.execute(
                "read_file",
                json.dumps({"root": "project", "path": str(workspace_root / "a.txt")}),
            )
            write_result = await runtime.tools_executor.execute(
                "write_file",
                '{"root":"project","path":"docs/a.txt","content":"hello"}',
            )
            read_result = await runtime.tools_executor.execute(
                "read_file",
                '{"root":"project","path":"docs/a.txt"}',
            )
            command_spec = runtime.tools_executor.registry.get("execute_command")

        self.assertEqual(missing_root["code"], "UNKNOWN_WORKSPACE_ROOT")
        self.assertEqual(absolute_path["code"], "ABSOLUTE_PATH_NOT_ALLOWED")
        self.assertEqual(write_result["code"], "FILE_CREATED")
        self.assertEqual(read_result["data"]["content"], "hello")
        self.assertIsNotNone(command_spec)

    async def test_runtime_router_is_the_default_runtime_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(directory)),
                workspace_roots={"project": Path.cwd()},
            )
            await self.start_application(application)
            application.create_session("session-1")

        runtime = application._session_runtime._session_run_runtimes[
            "session-1"
        ]
        self.assertIsNone(runtime.runtime_mode)
        self.assertIsInstance(runtime.mode_router, RuntimeModeRouter)
        self.assertIsInstance(runtime.runtime_modes[RunMode.PLAIN], PlainMode)
        self.assertIsInstance(runtime.runtime_modes[RunMode.TODO], TodoMode)

    async def test_explicit_runtime_mode_overrides_default(self):
        mode = PlainMode()
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(directory)),
                workspace_roots={"project": Path.cwd()},
                runtime_mode=mode,
            )
            await self.start_application(application)
            application.create_session("session-1")

        self.assertIs(
            application._session_runtime._session_run_runtimes[
                "session-1"
            ].runtime_mode,
            mode,
        )

    async def test_each_session_gets_a_private_run_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(directory)),
                workspace_roots={"project": Path.cwd()},
            )
            await self.start_application(application)
            application.create_session("session-a")
            application.create_session("session-b")

        runtimes = application._session_runtime._session_run_runtimes
        self.assertIsNot(runtimes["session-a"], runtimes["session-b"])
        self.assertIsNot(
            runtimes["session-a"].tool_result_externalizer.store,
            runtimes["session-b"].tool_result_externalizer.store,
        )

    async def test_read_artifact_is_bound_to_current_session_drawer(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(directory)),
                workspace_roots={"project": Path.cwd()},
            )
            await self.start_application(application)
            application.create_session("session-a")
            application.create_session("session-b")
            runtimes = application._session_runtime._session_run_runtimes
            ref = runtimes["session-a"].tool_result_externalizer.store.save(
                "A content"
            )

            own_result = await runtimes["session-a"].tools_executor.execute(
                "read_artifact",
                json.dumps({"artifact_id": ref.artifact_id}),
            )
            foreign_result = await runtimes["session-b"].tools_executor.execute(
                "read_artifact",
                json.dumps({"artifact_id": ref.artifact_id}),
            )

        self.assertEqual(own_result["data"]["content"], "A content")
        self.assertEqual(foreign_result["code"], "ARTIFACT_NOT_FOUND")

    async def test_delete_session_removes_its_artifact_drawer(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_agent_application(
                model="test-model",
                model_context_limit=10_000,
                agent_workspace=AgentWorkspace(Path(directory)),
                workspace_roots={"project": Path.cwd()},
            )
            await self.start_application(application)
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
