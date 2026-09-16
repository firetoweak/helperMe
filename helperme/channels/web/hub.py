from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebEvent:
    name: str
    data: dict


@dataclass(slots=True)
class _Preview:
    output_id: str
    text: str = ""


class WebEventHub:
    """Page-level fan-out. One live preview per Session; delivery never depends on browsers."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[WebEvent]] = set()
        self._previews: dict[str, _Preview] = {}

    def subscribe(self) -> asyncio.Queue[WebEvent]:
        queue: asyncio.Queue[WebEvent] = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[WebEvent]) -> None:
        self._queues.discard(queue)

    async def preview(
        self,
        session_id: str,
        phase: str,
        output_id: str,
        text: str | None,
    ) -> None:
        if phase == "started":
            self._previews[session_id] = _Preview(output_id)
            await self._broadcast(
                "preview.started",
                {"session_id": session_id, "output_id": output_id},
            )
            return
        if phase == "delta":
            preview = self._previews.get(session_id)
            if preview is None or preview.output_id != output_id:
                raise RuntimeError("preview output is not active")
            if type(text) is not str:
                raise TypeError("preview delta text must be str")
            preview.text += text
            await self._broadcast(
                "preview.delta",
                {
                    "session_id": session_id,
                    "output_id": output_id,
                    "text": text,
                },
            )
            return
        if phase == "aborted":
            preview = self._previews.pop(session_id, None)
            if preview is None:
                return
            if preview.output_id != output_id:
                raise RuntimeError("aborted output is not the active preview")
            await self._broadcast(
                "preview.aborted",
                {"session_id": session_id, "output_id": output_id},
            )
            return
        raise ValueError(f"unknown preview phase: {phase}")

    async def output_final(self, session_id: str, output_id: str, text: str) -> None:
        preview = self._previews.get(session_id)
        if preview is not None and preview.output_id != output_id:
            raise RuntimeError("delivered output is not the active preview")
        self._previews.pop(session_id, None)
        await self._broadcast(
            "output_final",
            {
                "session_id": session_id,
                "output_id": output_id,
                "text": text,
            },
        )

    async def session_activity(self, session_id: str, activity: str) -> None:
        await self._broadcast(
            "session_activity",
            {"session_id": session_id, "activity": activity},
        )

    async def context_usage(self, session_id: str, used: int, limit: int) -> None:
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if type(used) is not int or used < 0:
            raise ValueError("used must be a nonnegative int")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive int")
        await self._broadcast(
            "context_usage",
            {"session_id": session_id, "used": used, "limit": limit},
        )

    async def tool_progress(
        self,
        session_id: str,
        phase: str,
        command_id: str,
        name: str,
        _payload,
    ) -> None:
        if phase not in {"start", "fail", "finish"}:
            raise ValueError(f"unknown tool progress phase: {phase}")
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be a non-empty str")
        if type(command_id) is not str or not command_id:
            raise ValueError("command_id must be a non-empty str")
        if type(name) is not str or not name:
            raise ValueError("name must be a non-empty str")
        await self._broadcast(
            "tool_progress",
            {
                "session_id": session_id,
                "command_id": command_id,
                "name": name,
                "status": {"start": "running", "fail": "failed", "finish": "succeeded"}[
                    phase
                ],
            },
        )

    async def _broadcast(self, name: str, data: dict) -> None:
        event = WebEvent(name, data)
        for queue in tuple(self._queues):
            queue.put_nowait(event)
