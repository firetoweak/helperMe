from __future__ import annotations

from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Any

from core.session import SessionRunOutcome, SessionRuntime
from core.session.state import SessionStatus
from core.tools_runtime.run_invocation import RunInvocation
from core.approval import (
    ApprovalActionRegistry,
    ApprovalRequest,
    ApprovalResolution,
)

DEFAULT_MAX_ROUNDS = 50


class AgentApplication:
    def __init__(
        self,
        session_runtime: SessionRuntime,
        system_prompt: str,
        default_max_rounds: int = DEFAULT_MAX_ROUNDS,
        resources: tuple[AbstractAsyncContextManager[Any], ...] = (),
        approval_actions: ApprovalActionRegistry | None = None,
    ):
        if not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        if default_max_rounds < 1:
            raise ValueError("default_max_rounds 必须大于 0")

        self._session_runtime = session_runtime
        self._system_prompt = system_prompt
        self._default_max_rounds = default_max_rounds
        self._resources = resources
        self._resource_stack = AsyncExitStack()
        self._lifecycle_state = "new"
        self._approval_actions = approval_actions or ApprovalActionRegistry()

    async def __aenter__(self) -> "AgentApplication":
        if self._lifecycle_state != "new":
            raise RuntimeError(
                f"AgentApplication 不能进入，当前状态: {self._lifecycle_state}"
            )
        try:
            async with AsyncExitStack() as stack:
                for resource in self._resources:
                    await stack.enter_async_context(resource)
                self._resource_stack = stack.pop_all()
        except BaseException:
            self._lifecycle_state = "closed"
            raise
        self._lifecycle_state = "started"
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        self._require_started()
        self._ensure_no_active_runs()
        self._lifecycle_state = "closing"
        try:
            return await self._resource_stack.__aexit__(
                exc_type,
                exc,
                traceback,
            )
        finally:
            self._lifecycle_state = "closed"

    async def close(self) -> None:
        if self._lifecycle_state == "closed":
            return
        if self._lifecycle_state == "new":
            self._lifecycle_state = "closed"
            return
        self._ensure_no_active_runs()
        self._lifecycle_state = "closing"
        try:
            await self._resource_stack.aclose()
        finally:
            self._lifecycle_state = "closed"

    def _require_started(self) -> None:
        if self._lifecycle_state != "started":
            raise RuntimeError(
                "AgentApplication 必须在 async with 生命周期内使用；"
                f"当前状态: {self._lifecycle_state}"
            )

    def _ensure_no_active_runs(self) -> None:
        if self._session_runtime.active_controls:
            raise RuntimeError("AgentApplication 仍有活动 Run，不能关闭资源")

    def create_session(self, session_id: str) -> str:
        self._require_started()
        self._session_runtime.create_session(
            session_id=session_id,
            system_prompt=self._system_prompt,
        )
        return session_id

    async def start(
        self,
        session_id,
        run_id,
        message,
        max_rounds=None,
        *,
        invocation: RunInvocation | None = None,
    ):
        self._require_started()
        return await self._session_runtime.start(
            session_id,
            run_id,
            message,
            self._resolve_max_rounds(max_rounds),
            invocation=invocation,
        )

    async def resume(
        self,
        session_id,
        run_id,
        message,
        max_rounds=None,
        *,
        invocation: RunInvocation | None = None,
    ):
        self._require_started()
        return await self._session_runtime.resume(
            session_id,
            run_id,
            message,
            self._resolve_max_rounds(max_rounds),
            invocation=invocation,
        )

    def require_session(self, session_id: str) -> None:
        self._require_started()
        self._session_runtime.get_session(session_id)

    def pending_approval(
        self,
        session_id: str,
    ) -> ApprovalRequest | None:
        self._require_started()
        session = self._session_runtime.get_session(session_id)
        if session.pending_approval_id is None:
            return None
        return session.conversation.get_approval_request(
            session.pending_approval_id
        )

    async def resolve_approval(
        self,
        session_id: str,
        confirmation: str,
    ) -> ApprovalResolution:
        self._require_started()
        if confirmation not in {"yes", "no"}:
            raise ValueError("approval confirmation 必须是 yes 或 no")
        session = self._session_runtime.get_session(session_id)
        approval_id = session.pending_approval_id
        if approval_id is None:
            raise ValueError("Session 当前没有待审批请求")
        request = session.conversation.get_approval_request(approval_id)
        session.conversation.add_user(confirmation)
        if confirmation == "no":
            resolution = ApprovalResolution(
                approval_id=approval_id,
                decision="rejected",
            )
            event_message = (
                f"Approval `{approval_id}` 已被用户拒绝，未执行任何操作。"
            )
        else:
            execution = await self._approval_actions.execute(request)
            resolution = ApprovalResolution(
                approval_id=approval_id,
                decision="approved",
                execution=execution,
            )
            event_message = (
                f"Approval `{approval_id}` 执行结果：{execution.message}"
            )
        session.conversation.record_approval_resolution(resolution)
        session.conversation.add_system_event(event_message)
        session.pending_approval_id = None
        return resolution

    def validate_run(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
    ) -> None:
        self._require_started()
        self._session_runtime.validate_run_input(run_id, user_message)
        session = self._session_runtime.get_session(session_id)
        if session.status is SessionStatus.RUNNING:
            raise ValueError(f"Session 已在执行: {session_id}")
        if any(record.run_id == run_id for record in session.run_records):
            raise ValueError(f"重复 run_id: {run_id}")

    async def execute(
        self,
        session_id: str,
        run_id: str,
        user_message: str,
        max_rounds: int | None,
        invocation: RunInvocation,
    ) -> SessionRunOutcome:
        self._require_started()
        session = self._session_runtime.get_session(session_id)
        use_case = (
            self._session_runtime.resume
            if session.status is SessionStatus.INTERRUPTED
            else self._session_runtime.start
        )
        return await use_case(
            session_id,
            run_id,
            user_message,
            self._resolve_max_rounds(max_rounds),
            invocation=invocation,
        )

    def _resolve_max_rounds(self, max_rounds: int | None) -> int:
        resolved = (
            self._default_max_rounds
            if max_rounds is None
            else max_rounds
        )
        if resolved < 1:
            raise ValueError("max_rounds 必须大于 0")
        return resolved

    def request_interrupt(self, session_id, reason=None):
        self._require_started()
        self._session_runtime.request_interrupt(session_id, reason)

    def delete_session(self, session_id: str) -> None:
        self._require_started()
        self._session_runtime.delete_session(session_id)
