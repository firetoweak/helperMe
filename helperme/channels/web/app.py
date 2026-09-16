from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from helperme.assistant.host.session_store import (
    ForkMessageNotFoundError,
    SessionForkUnavailableError,
)
from helperme.assistant.runner import SessionNotFoundError
from helperme.bootstrap import bootstrap_assistant
from helperme.channels.web.channel import WebChannel
from helperme.channels.web.hub import WebEventHub


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str


class InputRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    connection_id: str
    delivery_id: str
    text: str


class EditRequest(InputRequest):
    message_id: str


def create_web_app(
    channel: WebChannel | None = None,
    hub: WebEventHub | None = None,
) -> FastAPI:
    events = hub if hub is not None else WebEventHub()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.hub = events
        if channel is not None:
            app.state.channel = channel
            yield
            return
        async with bootstrap_assistant(
            events.output_final,
            preview_sink=events.preview,
            session_activity_sink=events.session_activity,
            tool_progress_sink=events.tool_progress,
        ) as assistant:
            app.state.channel = WebChannel(assistant.sessions, assistant.queries)
            yield

    app = FastAPI(lifespan=lifespan)

    @app.exception_handler(SessionNotFoundError)
    async def session_not_found(_request: Request, error: SessionNotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(ForkMessageNotFoundError)
    async def fork_message_not_found(
        _request: Request, error: ForkMessageNotFoundError
    ):
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(SessionForkUnavailableError)
    async def fork_unavailable(
        _request: Request, error: SessionForkUnavailableError
    ):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.get("/api/events", response_class=EventSourceResponse)
    async def stream_events(request: Request):
        web = _channel(request)
        connection = web.connect()
        queue = _hub(request).subscribe()
        try:
            yield ServerSentEvent(
                event="connected",
                data={"connection_id": connection.connection_id},
            )
            while True:
                item = await queue.get()
                yield ServerSentEvent(event=item.name, data=item.data)
        finally:
            _hub(request).unsubscribe(queue)
            await web.disconnect(connection)

    @app.get("/api/sessions")
    async def sessions(request: Request):
        return await _channel(request).list_sessions()

    @app.get("/api/sessions/{session_id}")
    async def conversation(session_id: str, request: Request):
        return await _channel(request).conversation(session_id)

    @app.post("/api/sessions", status_code=201)
    async def create_session(body: ConnectionRequest, request: Request):
        return await _channel(request).create(body.connection_id)

    @app.post("/api/sessions/{session_id}/select")
    async def select_session(
        session_id: str,
        body: ConnectionRequest,
        request: Request,
    ):
        return await _channel(request).select(body.connection_id, session_id)

    @app.post("/api/sessions/{session_id}/inputs")
    async def accept_input(session_id: str, body: InputRequest, request: Request):
        return await _channel(request).accept_input(
            body.connection_id,
            session_id,
            body.text,
            body.delivery_id,
        )

    @app.post("/api/sessions/{session_id}/forks", status_code=201)
    async def edit_and_fork(
        session_id: str,
        body: EditRequest,
        request: Request,
    ):
        return await _channel(request).edit_and_fork(
            body.connection_id,
            session_id,
            body.message_id,
            body.text,
            body.delivery_id,
        )

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_session(
        session_id: str,
        body: ConnectionRequest,
        request: Request,
    ):
        return await _channel(request).cancel(body.connection_id, session_id)

    assets = Path(__file__).parents[3] / "web" / "dist"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets / "assets"), name="assets")

        @app.get("/{path:path}")
        async def frontend(path: str):
            candidate = assets / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(assets / "index.html")

    return app


def _channel(request: Request) -> WebChannel:
    return request.app.state.channel


def _hub(request: Request) -> WebEventHub:
    return request.app.state.hub
