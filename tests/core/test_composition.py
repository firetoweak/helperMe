import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from core.agent_workspace import AgentWorkspace
from core.composition import create_agent_application
from core.environment import (
    FilesystemAccessMode,
    RootBinding,
    WorkspaceScope,
)
from core.runtime_artifacts import ArtifactNotFoundError
from core.runtime_modes import PlainMode, TurnMode, RuntimeModeRouter
from core.todos import TodoMode
from core.tools_runtime.tools_executor import ToolsExecutor


class CompositionTest(unittest.IsolatedAsyncioTestCase):
    async def start_application(self, application):
        await application.__aenter__()
        self.addAsyncCleanup(application.close)
        return application

    async def test_scoped_access_does_not_discover_host_roots(self):
        with tempfile.TemporaryDirectory() as runtime_directory, patch(
            "core.composition.discover_host_roots"
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
                    "core.composition.discover_host_roots",
                    return_value=(RootBinding(
                        "drive_z",
                        WorkspaceScope.HOST,
                        Path(runtime_directory),
                    ),),
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
            selection = application._session_runtime.sessions[
                "session-1"
            ].default_environment_selection

        self.assertIsNotNone(selection)
        self.assertEqual(
            [root.root_id for root in selection.workspace_view.roots],
            ["project", "drive_z"],
        )

    async def test_host_access_rejects_explicit_root_name_collision(self):
        with tempfile.TemporaryDirectory() as runtime_directory, \
                tempfile.TemporaryDirectory() as workspace_directory, patch(
                    "core.composition.discover_host_roots",
                    return_value=(RootBinding(
                        "drive_d",
                        WorkspaceScope.HOST,
                        Path(workspace_directory),
                    ),),
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

    async def test_environment_tools_are_bound_from_turn_selection(self):
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
            runtime = application._session_runtime._session_turn_runtimes["session-1"]
            session_runtime = application._session_runtime
            selection = session_runtime.sessions[
                "session-1"
            ].default_environment_selection
            binding = await session_runtime.environment_provider.attach(
                selection
            )
            registry = runtime.tools_executor.registry.clone()
            for spec in runtime.environment_tool_factory(binding):
                registry.register(spec)
            executor = ToolsExecutor(registry)

            self.assertIsNone(runtime.tools_executor.registry.get("read_file"))
            absolute_path = await runtime.tools_executor.execute(
                "read_file",
                json.dumps({"path": str(workspace_root / "a.txt")}),
            )
            write_result = await executor.execute(
                "write_file",
                '{"path":"docs/a.txt","content":"hello"}',
            )
            read_result = await executor.execute(
                "read_file",
                '{"path":"docs/a.txt"}',
            )
            absolute_result = await executor.execute(
                "read_file",
                json.dumps({"path": str(workspace_root / "docs/a.txt")}),
            )
            command_spec = registry.get("execute_command")

        self.assertEqual(absolute_path["code"], "TOOL_NOT_FOUND")
        self.assertEqual(write_result["code"], "FILE_CREATED")
        self.assertEqual(read_result["data"]["content"], "hello")
        self.assertEqual(absolute_result["data"]["content"], "hello")
        self.assertEqual(
            absolute_result["data"]["location"]["environment_id"],
            "local",
        )
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

        runtime = application._session_runtime._session_turn_runtimes[
            "session-1"
        ]
        self.assertIsNone(runtime.runtime_mode)
        self.assertIsInstance(runtime.mode_router, RuntimeModeRouter)
        self.assertIsInstance(runtime.runtime_modes[TurnMode.PLAIN], PlainMode)
        self.assertIsInstance(runtime.runtime_modes[TurnMode.TODO], TodoMode)

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
            application._session_runtime._session_turn_runtimes[
                "session-1"
            ].runtime_mode,
            mode,
        )

    async def test_each_session_gets_a_private_turn_runtime(self):
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

        runtimes = application._session_runtime._session_turn_runtimes
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
            runtimes = application._session_runtime._session_turn_runtimes
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
            runtime = application._session_runtime._session_turn_runtimes[
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
