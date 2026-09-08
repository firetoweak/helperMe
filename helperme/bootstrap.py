from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from helperme.assistant.session_store import SessionStore
from helperme.assistant.supervisor import HostSupervisor
from helperme.config import AppConfig, assistant_config_from_app, load_app_config
from helperme.llm.client import LLMClient
from helperme.paths import HelperMeHome
from helperme.mcp.composition import build_mcp
from helperme.skills.composition import build_skills
from helperme.skills.summarizer import LlmSkillDiffSummarizer


def worker_config(app_config: AppConfig):
    return assistant_config_from_app(app_config, LLMClient(app_config.model))


@dataclass(frozen=True, slots=True)
class BootstrappedAssistant:
    config: AppConfig
    sessions: HostSupervisor
    sessions_root: Path
    mcp_service: object
    skill_service: object


@asynccontextmanager
async def bootstrap_assistant(
    sink: Callable[[str, str], None],
    *,
    app_config: AppConfig | None = None,
    context_usage_sink=None,
    subagent_activity_sink=None,
    conversation_status_sink=None,
) -> AsyncIterator[BootstrappedAssistant]:
    config = load_app_config() if app_config is None else app_config
    home = HelperMeHome.default()
    home.initialize()
    store = SessionStore(home.runtime_sessions_root)
    host = HostSupervisor(
        store,
        partial(worker_config, config),
        home,
        sink,
        context_usage_sink=context_usage_sink,
        subagent_activity_sink=subagent_activity_sink,
        conversation_status_sink=conversation_status_sink,
    )
    # Channel management is product-level; session tools get their own clients.
    management_llm = LLMClient(config.model)
    mcp = build_mcp(home)
    skills = build_skills(
        home, diff_summarizer=LlmSkillDiffSummarizer(management_llm, config.model.name)
    )
    async with management_llm, mcp.client_manager:
        try:
            yield BootstrappedAssistant(
                config, host, store.root, mcp.service, skills.service
            )
        finally:
            await host.close()
