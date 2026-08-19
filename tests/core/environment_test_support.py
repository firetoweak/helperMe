from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.environment import (
    EnvironmentBinding,
    EnvironmentSelection,
    LocalEnvironmentProvider,
    PermissionBinding,
    RootBinding,
    RuntimeAttachment,
    WorkspaceScope,
    WorkspaceViewSnapshot,
)
from core.session.runtime import SessionRuntime
from core.session.state import Session
from core.tools_runtime.turn_invocation import TurnInvocation
from core.tools_runtime.turn_runtime import TurnRuntime


class UnavailableCommandExecutor:
    async def run(self, command: str, cwd: Path, timeout_seconds: int):
        raise AssertionError("测试未预期执行 Environment command")


TEST_ROOT = Path(__file__).resolve().parents[2]
TEST_WORKSPACE_VIEW = WorkspaceViewSnapshot((
    RootBinding("project", WorkspaceScope.TASK, TEST_ROOT),
))
TEST_ENVIRONMENT_SELECTION = EnvironmentSelection(
    environment_id="test-local",
    workspace_view=TEST_WORKSPACE_VIEW,
    cwd=str(TEST_ROOT),
)
TEST_COMMAND_EXECUTOR = UnavailableCommandExecutor()
TEST_ENVIRONMENT_PROVIDER = LocalEnvironmentProvider(
    TEST_COMMAND_EXECUTOR,
    environment_id="test-local",
)
TEST_ENVIRONMENT_BINDING = EnvironmentBinding(
    environment_id="test-local",
    workspace_view=TEST_WORKSPACE_VIEW,
    permission_binding=PermissionBinding.read_write(TEST_WORKSPACE_VIEW),
    cwd=TEST_ROOT,
    shell_name="test-shell",
    shell_path="test-shell",
    runtime_attachment=RuntimeAttachment(
        environment_instance_id="test-local-instance",
        command_executor=TEST_COMMAND_EXECUTOR,
    ),
)


def bind_turn_invocation(
    invocation: TurnInvocation | None = None,
) -> TurnInvocation:
    if invocation is not None and invocation.environment_binding is not None:
        return invocation
    return replace(
        invocation or TurnInvocation(),
        environment_binding=TEST_ENVIRONMENT_BINDING,
    )


def session_runtime_environment() -> dict[str, object]:
    return {
        "environment_provider": TEST_ENVIRONMENT_PROVIDER,
        "default_environment_selection": TEST_ENVIRONMENT_SELECTION,
    }


class BoundTurnRuntime(TurnRuntime):
    async def run(self, *args, invocation=None, **kwargs):
        return await super().run(
            *args,
            invocation=bind_turn_invocation(invocation),
            **kwargs,
        )


class BoundSessionRuntime(SessionRuntime):
    def __init__(self, *args, **kwargs):
        for name, value in session_runtime_environment().items():
            kwargs.setdefault(name, value)
        super().__init__(*args, **kwargs)


class BoundSession(Session):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault(
            "default_environment_selection",
            TEST_ENVIRONMENT_SELECTION,
        )
        super().__init__(*args, **kwargs)
