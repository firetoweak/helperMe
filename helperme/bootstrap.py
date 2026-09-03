from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from helperme.assistant.assembly import build_assistant_assembly
from helperme.assistant.runner import SessionScheduler
from helperme.assistant.sessions import AssistantSessions
from helperme.config import (
    AppConfig,
    AssistantConfig,
    assistant_config_from_app,
    load_app_config,
)
from helperme.llm.client import LLMClient
from helperme.paths import runtime_data_root
from helperme.runtime import SqliteJournal


@dataclass(frozen=True, slots=True)
class BootstrappedAssistant:
    config: AssistantConfig
    sessions: AssistantSessions
    scheduler: SessionScheduler
    journal_path: Path
    mcp_service: object
    skill_service: object


@asynccontextmanager
async def bootstrap_assistant(
    sink: Callable[[str, str], None],
    *,
    config: AssistantConfig | None = None,
    app_config: AppConfig | None = None,
    context_usage_sink: Callable[[str, int, int], None] | None = None,
    subagent_activity_sink: Callable[[str, bool], None] | None = None,
) -> AsyncIterator[BootstrappedAssistant]:
    assert config is None or app_config is None
    if config is None:
        app_config = load_app_config() if app_config is None else app_config
        config = assistant_config_from_app(
            app_config,
            LLMClient(app_config.model),
        )
    effective_config = config
    journal_path = runtime_data_root() / "journal.sqlite"
    journal = SqliteJournal(journal_path)
    assembly = await build_assistant_assembly(
        effective_config,
        sink,
        journal,
        context_usage_sink=context_usage_sink,
        subagent_activity_sink=subagent_activity_sink,
    )
    async with effective_config.llm, assembly.mcp.client_manager:
        try:
            yield BootstrappedAssistant(
                config=effective_config,
                sessions=assembly.sessions,
                scheduler=assembly.scheduler,
                journal_path=journal_path,
                mcp_service=assembly.mcp.service,
                skill_service=assembly.skills.service,
            )
        finally:
            await assembly.scheduler.close()
