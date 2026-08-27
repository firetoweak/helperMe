from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from helperme.assistant.assembly import build_assistant_assembly
from helperme.assistant.decision import JournalBackedLlmDecisionMaker
from helperme.assistant.sessions import AssistantSessions
from helperme.config import (
    AppConfig,
    AssistantConfig,
    assistant_config_from_app,
    load_app_config,
)
from helperme.llm.client import LLMClient
from helperme.paths import runtime_data_root
from helperme.runtime import AgentRuntime, SqliteJournal


@dataclass(frozen=True, slots=True)
class BootstrappedAssistant:
    config: AssistantConfig
    sessions: AssistantSessions
    journal_path: Path
    mcp_service: object
    skill_service: object


@asynccontextmanager
async def bootstrap_assistant(
    sink: Callable[[str], None],
    *,
    config: AssistantConfig | None = None,
    app_config: AppConfig | None = None,
    context_usage_sink: Callable[[str, int, int], None] | None = None,
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
    assembly = await build_assistant_assembly(effective_config, sink)
    runtime = AgentRuntime(
        journal,
        JournalBackedLlmDecisionMaker(
            journal,
            effective_config.llm,
            effective_config.model_name,
            surface=assembly.surface,
            skill_tools=assembly.skill_tools,
            projector=assembly.projector,
            control=assembly.control,
            management=assembly.management,
            context_usage_sink=context_usage_sink,
        ),
        assembly.bindings,
    )
    assembly.surface.attach(runtime)
    sessions = AssistantSessions(
        runtime,
        assembly.surface,
        control=assembly.control,
        management=assembly.management,
    )
    async with effective_config.llm, assembly.mcp.client_manager:
        yield BootstrappedAssistant(
            config=effective_config,
            sessions=sessions,
            journal_path=journal_path,
            mcp_service=assembly.mcp.service,
            skill_service=assembly.skills.service,
        )
