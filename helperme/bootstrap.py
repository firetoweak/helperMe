from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from helperme.assistant.host.process_env import install_host_environment
from helperme.assistant.host.session_store import SessionStore
from helperme.assistant.host.supervisor import HostSupervisor
from helperme.assistant.conversations import AssistantQueries
from helperme.assistant.delivery import DeliverySink, PreviewSink
from helperme.config import AppConfig, assistant_config_from_app, load_app_config
from helperme.llm.adapter import LiteLLMAdapter
from helperme.paths import HelperMeHome
from helperme.mcp.composition import build_mcp
from helperme.skills.composition import build_skills
from helperme.skills.summarizer import LlmSkillDiffSummarizer


class UnboundHostLlm:
    async def chat(self, *args, **kwargs):
        raise RuntimeError("Worker must bind the Host LLM port")


def worker_config(app_config: AppConfig):
    return assistant_config_from_app(app_config, UnboundHostLlm())


@dataclass(frozen=True, slots=True)
class BootstrappedAssistant:
    config: AppConfig
    sessions: HostSupervisor
    sessions_root: Path
    mcp_service: object
    skill_service: object
    queries: AssistantQueries


@asynccontextmanager
async def bootstrap_assistant(
    sink: DeliverySink,
    *,
    app_config: AppConfig | None = None,
    context_usage_sink=None,
    subagent_activity_sink=None,
    conversation_status_sink=None,
    tool_progress_sink=None,
    authorization_required_sink=None,
    preview_sink: PreviewSink | None = None,
    thinking_sink=None,
    session_activity_sink=None,
    session_failed_sink=None,
) -> AsyncIterator[BootstrappedAssistant]:
    install_host_environment()
    config = load_app_config() if app_config is None else app_config
    home = HelperMeHome.default()
    home.initialize()
    store = SessionStore(home.runtime_sessions_root)
    llm = LiteLLMAdapter(config.model, config.litellm)
    host = HostSupervisor(
        store,
        partial(worker_config, config),
        home,
        sink,
        llm=llm,
        context_usage_sink=context_usage_sink,
        subagent_activity_sink=subagent_activity_sink,
        conversation_status_sink=conversation_status_sink,
        tool_progress_sink=tool_progress_sink,
        authorization_required_sink=authorization_required_sink,
        preview_sink=preview_sink,
        thinking_sink=thinking_sink,
        session_activity_sink=session_activity_sink,
        session_failed_sink=session_failed_sink,
    )
    mcp = build_mcp(home)
    skills = build_skills(
        home, diff_summarizer=LlmSkillDiffSummarizer(llm, config.model.active)
    )
    async with llm, mcp.client_manager:
        try:
            yield BootstrappedAssistant(
                config,
                host,
                store.root,
                mcp.service,
                skills.service,
                AssistantQueries(store, host),
            )
        finally:
            await host.close()
