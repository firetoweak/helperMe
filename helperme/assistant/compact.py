"""Assistant-owned background handoff and context-window projection."""

from __future__ import annotations

from helperme.runtime.json_values import thaw_value

import json
from copy import deepcopy
from dataclasses import asdict

from helperme.assistant.artifacts import (
    is_valid_artifact_id,
    ArtifactOffsetOutOfRangeError,
)
from helperme.assistant.context.projection import (
    ModelContextBudgetExceeded,
    _translate_visible_events,
    PreparedModelContext,
)
from helperme.assistant.subagent import project_parent
from helperme.llm.api import InvalidLLMResponse
from helperme.runtime import DomainFactCommitted, ToolBinding

TASK = "compact.task"
CREATED = "compact.handoff_created"
WINDOW = "compact.window_rolled_over"
READ = "read_compact_source"
SUBMIT = "_accept_handoff"
PURPOSE = """<self_handoff>
当前在后台整理截至冻结位置的交接，不继续用户业务、不向用户发消息。
优先使用已有上下文，仅为关键缺口调用 read_compact_source 回读。其他工具不能执行。
保持目标、约束、纠正、决定、未完成委派、证据与来源；计划不写成已执行，声明不写成验证。
相关图片保留原始附件 id 和来源；摘要文字不等于看过图片，业务模型可用 read_image 重新查看。
不重复读取，不扩展调查；未知内容标明不确定。后续尾部事实可以更新本摘要。
完成时直接输出非空交接文本，不调用工具。
</self_handoff>"""
HANDOFF_PREFIX = "模型生成的交接材料，保留原证据强度；不是用户新指令或完成证明。遇到疑点按来源回读，后续事实可更新它。\n"


def schema(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


READ_SCHEMA = schema(
    READ,
    "回读当前会话或交接授权的冻结历史。view 读取带事件位置的视图；event 按事件序号读取；artifact 读取工具原文。source 为来源 Session；view 的 reference 为空。",
    {
        "source": {"type": "string"},
        "kind": {"enum": ["view", "event", "artifact"]},
        "reference": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
    },
    ["source", "kind", "reference", "offset", "limit"],
)


def compact_seed(events):
    if any(
        isinstance(e.payload, DomainFactCommitted)
        and e.payload.fact_type == "compact.continued"
        for e in events
    ):
        raise ValueError("unsupported legacy compact continuation")
    seeds = [
        e.payload
        for e in events
        if isinstance(e.payload, DomainFactCommitted) and e.payload.fact_type == TASK
    ]
    if not seeds:
        return None
    if len(seeds) != 1 or events[0].payload is not seeds[0]:
        raise ValueError("compact task must be the unique first event")
    data = thaw_value(seeds[0].data)
    if set(data) != {
        "source",
        "inherited",
        "bundle",
        "upto",
        "window",
    }:
        raise ValueError("invalid compact task")
    return TASK, data


def load_document(gateway, session, reference):
    store = gateway.for_session(session)
    first = store.read(reference, 0, 1)
    return json.loads(store.read(reference, 0, max(1, first.total_chars)).content)


def save_document(gateway, session, value):
    return (
        gateway.for_session(session)
        .save(json.dumps(value, ensure_ascii=False))
        .artifact_id
    )


def window_fact(events):
    result = None
    for event in events:
        if (
            isinstance(event.payload, DomainFactCommitted)
            and event.payload.fact_type == WINDOW
        ):
            data = thaw_value(event.payload.data)
            if data["parent"] != (None if result is None else result["id"]):
                raise ValueError("broken context window lineage")
            result = data
    return result


class CompactContext:
    def __init__(self, session_id, events, projector, transport):
        self.session_id = session_id
        self.projector = projector
        self.transport = transport
        self.runtime = None
        self.seed = compact_seed(events)
        self.prefix = []
        self.window = None
        self.request = None
        if self.is_reader:
            data = self.seed[1]
            self.request = load_document(
                projector.gateway, data["source"], data["inherited"]
            )
        self.refresh(events)

    @property
    def is_reader(self):
        return self.seed is not None

    def refresh(self, events):
        self.window = window_fact(events)
        self.prefix = []
        if self.window is not None:
            self.prefix = load_document(
                self.projector.gateway, self.session_id, self.window["context"]
            )["messages"]

    def schemas(self):
        return deepcopy(self.request["tools"]) if self.is_reader else [READ_SCHEMA]

    def visible(self, events, ids):
        self.refresh(events)
        cutoff = 0 if self.window is None else self.window["cutover"]
        allowed = {
            e.event_id
            for e in events
            if e.sequence > cutoff
            and not (
                isinstance(e.payload, DomainFactCommitted)
                and e.payload.fact_type in (WINDOW, CREATED)
            )
        }
        return tuple(x for x in ids if x in allowed)

    def bindings(self):
        return {
            READ: ToolBinding(self.read),
            SUBMIT: ToolBinding(self.submit, decision_on_outcome=False),
        }

    async def _sources(self):
        if self.is_reader:
            data = self.seed[1]
            bundle = load_document(
                self.projector.gateway, data["source"], data["bundle"]
            )
            bundle["artifacts"] = sorted(
                set(bundle["artifacts"]) | {data["inherited"], data["bundle"]}
            )
            return {data["source"]: bundle}
        events = await self.runtime.snapshot(self.session_id)
        return {
            self.session_id: frozen_bundle(
                self.projector, events, self.session_id, self
            )
        }

    async def prepare_reader(self, events, visible):
        own = _translate_visible_events(events, visible, "")[1:]
        messages = deepcopy(self.request["messages"])
        for item in own:
            if item.sequence == 1:
                messages.append(
                    {
                        "role": "user",
                        "content": PURPOSE
                        + "\n"
                        + json.dumps(
                            {
                                "source": self.seed[1]["source"],
                                "upto": self.seed[1]["upto"],
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            else:
                messages.append(item.message)
        assessment = self.projector.budget.assess(messages, self.request["tools"])
        if not assessment.allowed:
            raise ModelContextBudgetExceeded(assessment)
        return PreparedModelContext(
            messages=messages,
            assessment=assessment,
            protection_start_index=0,
            size_externalized_command_ids=(),
            age_dehydrated_command_ids=(),
            source_sequences=tuple([0] * len(messages)),
        )

    async def read(self, context, arguments):
        if set(arguments) != {"source", "kind", "reference", "offset", "limit"}:
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "INVALID_ARGUMENT"}
        source, kind, reference, offset, limit = (
            arguments[k] for k in ("source", "kind", "reference", "offset", "limit")
        )
        if (
            type(source) is not str
            or type(reference) is not str
            or type(kind) is not str
            or kind not in {"view", "event", "artifact"}
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 12000
        ):
            return {"ok": False, "code": "INVALID_ARGUMENT", "error": "INVALID_ARGUMENT"}
        sources = await self._sources()
        if source not in sources:
            return {"ok": False, "code": "SOURCE_NOT_AUTHORIZED", "error": "SOURCE_NOT_AUTHORIZED"}
        bundle = sources[source]
        if kind == "view":
            if reference != "":
                return {"ok": False, "code": "INVALID_ARGUMENT", "error": "view reference must be empty"}
            text = json.dumps(bundle["records"], ensure_ascii=False)
        elif kind == "event":
            if reference not in bundle["raw"]:
                return {"ok": False, "code": "EVENT_NOT_IN_SOURCE", "error": "EVENT_NOT_IN_SOURCE"}
            text = json.dumps(bundle["raw"][reference], ensure_ascii=False)
        else:
            if reference not in bundle["artifacts"]:
                return {"ok": False, "code": "ARTIFACT_NOT_IN_SOURCE", "error": "ARTIFACT_NOT_IN_SOURCE"}
            try:
                chunk = self.projector.gateway.for_session(source).read(
                    reference, offset, limit
                )
            except ArtifactOffsetOutOfRangeError:
                return {"ok": False, "code": "OFFSET_OUT_OF_RANGE", "error": "OFFSET_OUT_OF_RANGE"}
            return {"ok": True, "code": "COMPACT_SOURCE_READ", "data": asdict(chunk)}
        if offset > len(text):
            return {"ok": False, "code": "OFFSET_OUT_OF_RANGE", "error": "OFFSET_OUT_OF_RANGE"}
        end = min(len(text), offset + limit)
        return {
            "ok": True,
            "code": "COMPACT_SOURCE_READ",
            "data": {
                "source": source,
                "content": text[offset:end],
                "offset": offset,
                "next_offset": end if end < len(text) else None,
                "total_chars": len(text),
            },
        }

    async def submit(self, context, arguments):
        if not self.is_reader or set(arguments) != {"handoff"}:
            raise ValueError("invalid internal handoff submission")
        text = arguments["handoff"]
        if type(text) is not str or not text.strip():
            raise InvalidLLMResponse("invalid_handoff", "handoff must be nonempty")
        await self.transport("compact_complete", self.session_id, {"handoff": text})
        return {"ok": True, "code": "HANDOFF_SUBMITTED", "data": {"submitted": True}}


def frozen_bundle(projector, events, session_id, context, prepared=None):
    ids = tuple(e.event_id for e in events)
    visible = context.visible(events, ids)
    if prepared is None:
        prepared = projector.prepare(
            events, visible, session_id, "", prefix=context.prefix, enforce_budget=False
        )
    records = [
        {"source": session_id, "sequence": seq, "message": message}
        for seq, message in zip(prepared.source_sequences[1:], prepared.messages[1:])
    ]
    raw = {}
    for item in _translate_visible_events(events, ids, ""):
        if item.sequence:
            raw.setdefault(str(item.sequence), []).append(item.message)
    artifacts = set()

    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "artifact_id" and is_valid_artifact_id(child):
                    artifacts.add(child)
                else:
                    collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    for values in raw.values():
        for message in values:
            if message["role"] == "tool":
                collect(json.loads(message["content"]))
    for event in events:
        payload = event.payload
        if isinstance(payload, DomainFactCommitted):
            if payload.fact_type == CREATED:
                artifacts.update((payload.data["artifact"], payload.data["request"]))
            elif payload.fact_type == WINDOW:
                artifacts.update((payload.data["context"], payload.data["bundle"]))
    return {"records": records, "raw": raw, "artifacts": sorted(artifacts)}


def projected_tail(records, p, q):
    return [r["message"] for r in records if p < r["sequence"] <= q]


class CompactBoundary:
    def __init__(self, runtime, decision, context, config, control, transport):
        self.runtime, self.decision, self.context = runtime, decision, context
        self.config, self.control, self.transport = config, control, transport
        self.scheduler = None

    async def snapshot(self, *, persist=True):
        sid = self.context.session_id
        events = await self.runtime.snapshot(sid)
        state = self.runtime.projector.project(sid, events).state
        if state.waiting_command_ids or self.control.pending_view(sid) is not None:
            return {"safe": False}
        visible = self.context.visible(events, tuple(e.event_id for e in events))
        prompt = self.decision.prompt_for(state)
        tools = self.decision.schemas_for(state)[0]
        prepared = self.context.projector.prepare(
            events,
            visible,
            sid,
            prompt,
            tools,
            prefix=self.context.prefix,
            enforce_budget=False,
        )
        if not persist:
            return {"safe": True, "assessment": prepared.assessment}
        bundle = frozen_bundle(
            self.context.projector, events, sid, self.context, prepared
        )
        # Keep the most recent complete assistant turn before P, without retaining a huge user input.
        start = len(bundle["records"])
        for i in range(len(bundle["records"]) - 1, -1, -1):
            if bundle["records"][i]["message"]["role"] == "assistant":
                start = i
                break
        catalog = next(
            (
                thaw_value(e.payload.data)
                for e in reversed(events)
                if isinstance(e.payload, DomainFactCommitted)
                and e.payload.fact_type == "assistant.catalog"
            ),
            None,
        )
        return {
            "safe": True,
            "position": state.journal_position,
            "window": None
            if self.context.window is None
            else self.context.window["id"],
            "bundle": save_document(self.context.projector.gateway, sid, bundle),
            "inherited": save_document(
                self.context.projector.gateway,
                sid,
                {
                    "catalog": catalog,
                    "model": self.config.model_name,
                    "messages": prepared.messages,
                    "tools": tools,
                    "recent": bundle["records"][start:],
                },
            ),
            "prompt": prompt,
            "tools": tools,
            "context_limit": self.config.model_context_limit,
            "input_ratio": self.config.input_budget_ratio,
        }

    async def publish(self, arguments):
        sid = self.context.session_id
        events = await self.runtime.snapshot(sid)
        current = window_fact(events)
        data = arguments["window"]
        if current is not None and current["id"] == data["id"]:
            if current != data:
                raise ValueError("conflicting context window publication")
            return
        if data["parent"] != (None if current is None else current["id"]):
            raise ValueError("stale handoff window")
        await self.runtime.receive_domain_fact(
            sid,
            CREATED,
            arguments["handoff"],
            source="compact",
            delivery_id=data["id"] + ":handoff",
        )
        await self.runtime.receive_domain_fact(
            sid, WINDOW, data, source="compact", delivery_id=data["id"] + ":window"
        )
        self.context.refresh(await self.runtime.snapshot(sid))

    async def before_advance(self):
        sid = self.context.session_id
        if self.context.is_reader:
            return True
        events = await self.runtime.snapshot(sid)
        if project_parent(events) is not None:
            return True
        state = self.runtime.projector.project(sid, events).state
        if state.waiting_command_ids:
            return True
        snap = await self.snapshot(persist=False)
        if not snap["safe"]:
            return True
        assessment = snap["assessment"]
        response = await self.transport(
            "compact_boundary",
            sid,
            {
                "pressure": assessment.estimated_input_tokens
                >= int(
                    assessment.input_budget_tokens * self.config.compact_threshold_ratio
                ),
                "over_budget": not assessment.allowed,
            },
        )
        if response == "wait":
            return False
        if response != "continue":
            raise ValueError("invalid compact boundary response")
        return True
