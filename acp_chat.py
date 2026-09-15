from __future__ import annotations

import asyncio
import sys

from acp import run_agent

from helperme.bootstrap import bootstrap_assistant
from helperme.channels.acp import HelperMeAcpAgent
from helperme.config import InitialConfigCreated


async def async_main() -> None:
    agent: HelperMeAcpAgent | None = None

    def _push(coro) -> None:
        asyncio.get_running_loop().create_task(coro)

    async def sink(session_id: str, output_id: str, text: str) -> None:
        assert agent is not None
        await agent.deliver(session_id, output_id, text)

    async def preview(session_id: str, phase: str, output_id: str, text) -> None:
        assert agent is not None
        await agent.preview(session_id, phase, output_id, text)

    def report_usage(session_id: str, used: int, limit: int) -> None:
        assert agent is not None
        _push(agent.report_usage(session_id, used, limit))

    def report_tool(*values) -> None:
        assert agent is not None
        _push(agent.report_tool(*values))

    async with bootstrap_assistant(
        sink,
        context_usage_sink=report_usage,
        tool_progress_sink=report_tool,
        preview_sink=preview,
    ) as app:
        agent = HelperMeAcpAgent(app.sessions, app.config.workspace)
        try:
            await run_agent(agent)
        finally:
            await agent.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except InitialConfigCreated as error:
        print(error, file=sys.stderr)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
