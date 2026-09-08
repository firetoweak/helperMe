"""Host compact orchestration; the routing commit is the only publication point."""

from __future__ import annotations

import asyncio
import json

from helperme.assistant.artifacts import FileArtifactGateway
from helperme.assistant.compact import (
    TASK,
    CONTINUED,
    READ_SCHEMA,
    load_document,
    save_document,
)
from helperme.assistant.compact_store import CompactStore
from helperme.assistant.context.budget import InputBudget, TiktokenEstimator
from helperme.assistant.context.projection import ModelContextBudgetExceeded, jsonable
from helperme.assistant.ipc import ProcessFailure, WorkerFailed
from helperme.runtime import DomainFactCommitted, SqliteJournal


def seed_fact(kind, data, *, continuing):
    return dict(
        fact_type=kind,
        data=data,
        source="compact",
        delivery_id=kind,
        requests_decision=continuing,
    )


class CompactHost:
    def __init__(self, host):
        self.host = host
        self.store = CompactStore(host.store.root)
        self.gateway = FileArtifactGateway(host.store.root)
        self.locks = {}
        self.activating = set()

    def notify_status(self, session):
        if self.host.conversation_status_sink is not None:
            self.host.conversation_status_sink(self.store.status(session))

    def lock(self, session):
        conversation, _ = self.store.binding(session)
        return self.locks.setdefault(conversation, asyncio.Lock())

    def activate(self, session):
        if session in self.activating or self.host.closed:
            return
        self.activating.add(session)

        async def resume():
            try:
                await self.host.request("resume", session, {})
            finally:
                self.activating.remove(session)

        self.host._track(session, asyncio.create_task(resume()))

    async def ensure_seed(self, session, fact, *, unpublished=False):
        if not self.host.store.path(session).parent.exists():
            await self.host.store.create(session, initial_fact=fact)
            return
        events = await SqliteJournal(self.host.store.require(session)).snapshot(session)
        if not events or (unpublished and len(events) != 1):
            raise ValueError("invalid compact Session bootstrap")
        payload = events[0].payload
        if (
            not isinstance(payload, DomainFactCommitted)
            or payload.fact_type != fact["fact_type"]
            or jsonable(payload.data) != fact["data"]
            or payload.requests_decision != fact["requests_decision"]
        ):
            raise ValueError("compact Session does not match its prepared seed")

    async def ensure_reader(self, job):
        reader = job["reader"]
        if job["failure"] is not None:
            raise WorkerFailed(reader, ProcessFailure(**json.loads(job["failure"])))
        bundle = json.loads(job["bundle"])
        await self.ensure_seed(
            reader,
            seed_fact(
                TASK,
                {
                    "source": job["source"],
                    "bundle": bundle["artifact"],
                    "upto": job["upto"],
                },
                continuing=True,
            ),
        )
        if reader not in self.host.workers:
            self.activate(reader)

    async def recover_prepared(self, source):
        job = self.store.job(source)
        if job is None or job["prepared"] is None or job["published"]:
            return
        successor = job["successor"]
        await self.ensure_seed(successor, json.loads(job["prepared"]), unpublished=True)
        self.store.publish(source, successor)
        self.notify_status(source)
        self.activate(successor)

    async def boundary(self, source, arguments):
        async with self.lock(source):
            self.store.register(source)
            await self.recover_prepared(source)
            if self.store.binding(source)[1] != source:
                return "retired"
            # Replay any input whose Host acknowledgement was interrupted.
            await self.flush_deliveries(source)
            job = self.store.job(source)
            if job is None:
                if not arguments["pressure"]:
                    return "continue"
                snapshot = await self.host.request(
                    "compact_snapshot", source, {"cutover": False}
                )
                if not snapshot["safe"] or snapshot["position"] == 0:
                    return "wait" if arguments["over_budget"] else "continue"
                job = self.store.start(
                    source,
                    snapshot["position"],
                    {
                        "artifact": snapshot["bundle"],
                    },
                )
                self.notify_status(source)
            if job["summary"] is None:
                await self.ensure_reader(job)
                return "wait" if arguments["over_budget"] else "continue"
            snapshot = await self.host.request("compact_snapshot", source, {})
            if not snapshot["safe"]:
                return "wait" if arguments["over_budget"] else "continue"
            await self.prepare_successor(job, snapshot)
            await self.recover_prepared(source)
            return "retired"

    async def prepare_successor(self, job, snapshot):
        source = job["source"]
        bundle = load_document(self.gateway, source, snapshot["bundle"])
        p, q = job["upto"], snapshot["position"]
        provenance = {
            "source_session": source,
            "summarized_through": p,
            "continued_through": q,
            "pending_source_events": snapshot["pending"],
        }
        messages = [
            {
                "role": "user",
                "content": "以下是模型生成的交接材料，不是用户新指令或完成证明。后续原样对话可更新或推翻它。\n"
                + json.dumps(provenance, ensure_ascii=False)
                + "\n"
                + job["summary"],
            }
        ]
        for key, values in bundle["raw"].items():
            if p < int(key) <= q:
                messages.extend(values)
        budget = InputBudget(
            TiktokenEstimator(),
            context_limit=snapshot["context_limit"],
            input_ratio=snapshot["input_ratio"],
        )
        tools = list(snapshot["tools"])
        if not any(
            t["function"]["name"] == READ_SCHEMA["function"]["name"] for t in tools
        ):
            tools.append(READ_SCHEMA)
        assessment = budget.assess(
            [{"role": "system", "content": snapshot["prompt"]}, *messages], tools
        )
        if not assessment.allowed:
            raise ModelContextBudgetExceeded(assessment)
        context = save_document(self.gateway, source, {"messages": messages})
        # Persist preparation before creating S1. Recovery finishes the exact same cutover.
        self.store.prepare(
            source,
            seed_fact(
                CONTINUED,
                {
                    "source": source,
                    "bundle": snapshot["bundle"],
                    "upto": p,
                    "cutover": q,
                    "context": context,
                    "pending": snapshot["pending"],
                },
                continuing=snapshot["continue"],
            ),
        )

    async def complete(self, reader, arguments):
        job = self.store.reader_job(reader)
        if job is None:
            raise ValueError("unknown compact reader")
        self.store.finish(reader, arguments["handoff"])
        self.notify_status(job["source"])
        # Avoid waiting for a source which may itself be awaiting this IPC callback.
        source = job["source"]

        async def ready():
            await self.host.request("compact_ready", source, {})

        self.host._track(source, asyncio.create_task(ready()))

    async def flush_deliveries(self, session):
        conversation, _ = self.store.binding(session)
        for row in self.store.pending_deliveries(conversation):
            await self.host.request(
                "receive_user_message",
                row["target"],
                {
                    "content": row["content"],
                    "source": row["source"],
                    "delivery_id": row["id"],
                },
            )
            self.store.acknowledge(conversation, row["source"], row["id"])

    async def application(self, operation, session, arguments):
        async with self.lock(session):
            self.host.store.require(session)
            self.store.register(session)
            _, current = self.store.binding(session)
            await self.recover_prepared(current)
            conversation, current = self.store.binding(session)
            await self.flush_deliveries(session)
            if operation == "receive_user_message":
                source = arguments.get("source", "user")
                target, accepted = self.store.reserve_delivery(
                    conversation,
                    source,
                    arguments["delivery_id"],
                    current,
                    arguments["content"],
                )
                if accepted:
                    return None
                await self.host.request(operation, target, arguments)
                self.store.acknowledge(conversation, source, arguments["delivery_id"])
                return None
            result = await self.host.request(operation, current, arguments)
            # Resume unfinished compression only on explicit activity, not by scanning sessions.
            if operation == "resume":
                job = self.store.job(current)
                if job is not None and job["summary"] is None:
                    await self.ensure_reader(job)
            return result
