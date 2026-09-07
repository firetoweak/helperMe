"""Compact reading, handoff and Worker-side scheduling. No Runtime semantics."""

from __future__ import annotations

from dataclasses import asdict
import json

from helperme.assistant.artifacts import (
    is_valid_artifact_id,
    ArtifactOffsetOutOfRangeError,
)
from helperme.assistant.context.projection import (
    ModelContextBudgetExceeded,
    jsonable,
    _translate_visible_events,
)
from helperme.assistant.subagent import project_parent, project_pending
from helperme.llm.api import InvalidLLMResponse
from helperme.runtime import DomainFactCommitted, ToolBinding
from helperme.runtime.events import UserMessageReceived
from helperme.runtime.model import RuntimeStatus


TASK = "compact.task"
CONTINUED = "compact.continued"
READ = "read_compact_source"
SUBMIT = "submit_handoff"
PROMPT = """你是一次性的上下文压缩者，任务是为接替模型写 handoff，不执行原用户任务。
先用 read_compact_source 顺序读完 source 指定的脱水对话（view），按 next_offset 分页。
保持对话顺序理解确认、纠正和否决；需要时用 event 展开原消息，artifact 展开工具结果。
再提交四部分交接文档：用户要什么；目前做到哪里；还有什么未解决；接手所需的证据与入口。
用户要求最详细（关键措辞和出处），模型行动次之，工具输出保留关键证据及回读位置。
区分用户决定、模型建议、已执行动作和完成声明。模型声称完成不等于已经验证。
只描述截至 source 的固定历史；后续原样 tail 可更新或推翻此文档。
用 submit_handoff 交付，不能直接答复用户。压缩者没有写工作区、委派或其他业务工具。
不要逐条复述历史；在保留接手所需信息的前提下尽量简洁。
"""


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
    "只读交接记录授权的前序来源。view 按顺序读脱水对话；event 读原消息；artifact 读完整工具结果。"
    "source 必须来自交接引用。offset/limit 为字符位置，按 next_offset 继续。",
    {
        "source": {"type": "string"},
        "kind": {"enum": ["view", "event", "artifact"]},
        "reference": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 12000},
    },
    ["source", "kind", "reference", "offset", "limit"],
)
SUBMIT_SCHEMA = schema(
    SUBMIT,
    "提交最终 handoff 交接文档，完成压缩。",
    {"handoff": {"type": "string", "minLength": 1}},
    ["handoff"],
)


def compact_seed(events):
    seeds = [
        e.payload
        for e in events
        if isinstance(e.payload, DomainFactCommitted)
        and e.payload.fact_type in (TASK, CONTINUED)
    ]
    if not seeds:
        return None
    if len(seeds) != 1 or events[0].payload is not seeds[0]:
        raise ValueError("compact seed must be the unique first event")
    seed = seeds[0]
    data = jsonable(seed.data)
    expected = (
        {"source", "bundle", "upto"}
        if seed.fact_type == TASK
        else {"source", "bundle", "upto", "cutover", "context", "pending"}
    )
    if set(data) != expected:
        raise ValueError("invalid compact seed fields")
    for key in ("source", "bundle"):
        if type(data[key]) is not str or not data[key]:
            raise ValueError(f"invalid compact {key}")
    if not is_valid_artifact_id(data["bundle"]):
        raise ValueError("invalid compact bundle")
    if type(data["upto"]) is not int or data["upto"] < 1:
        raise ValueError("invalid compact cutoff")
    if seed.fact_type == CONTINUED:
        if type(data["cutover"]) is not int or data["cutover"] < data["upto"]:
            raise ValueError("invalid compact cutover")
        if not is_valid_artifact_id(data["context"]):
            raise ValueError("invalid compact context")
        if type(data["pending"]) is not list or any(
            type(x) is not str for x in data["pending"]
        ):
            raise ValueError("invalid pending source events")
    return seed.fact_type, data


def load_document(gateway, session, reference):
    # The references are trusted persisted bindings; absent/corrupt data must fail.
    store = gateway.for_session(session)
    first = store.read(reference, 0, 1)
    return json.loads(store.read(reference, 0, max(1, first.total_chars)).content)


def save_document(gateway, session, value):
    return (
        gateway.for_session(session)
        .save(json.dumps(value, ensure_ascii=False))
        .artifact_id
    )


class CompactContext:
    def __init__(self, session_id, events, projector, transport):
        self.session_id = session_id
        self.projector = projector
        self.transport = transport
        self.seed = compact_seed(events)
        self.prefix = []
        if self.seed is not None and self.seed[0] == CONTINUED:
            data = self.seed[1]
            document = load_document(projector.gateway, data["source"], data["context"])
            if set(document) != {"messages"} or type(document["messages"]) is not list:
                raise ValueError("invalid compact context document")
            self.prefix = document["messages"]

    @property
    def is_reader(self):
        return self.seed is not None and self.seed[0] == TASK

    def schemas(self):
        if self.seed is None:
            return []
        return [READ_SCHEMA, SUBMIT_SCHEMA] if self.is_reader else [READ_SCHEMA]

    def visible(self, events, ids):
        hidden = {
            e.event_id
            for e in events
            if isinstance(e.payload, DomainFactCommitted)
            and e.payload.fact_type == CONTINUED
        }
        return tuple(x for x in ids if x not in hidden)

    def bindings(self):
        return {
            READ: ToolBinding(self.read),
            SUBMIT: ToolBinding(self.submit, decision_on_outcome=False),
        }

    def _sources(self):
        if self.seed is None:
            return {}
        data = self.seed[1]
        pending = [(data["source"], data["bundle"])]
        sources = {}
        while pending:
            source, reference = pending.pop()
            if source in sources:
                raise ValueError("cyclic compact lineage")
            bundle = load_document(self.projector.gateway, source, reference)
            if set(bundle) != {"records", "raw", "artifacts", "previous"}:
                raise ValueError("invalid compact source bundle")
            sources[source] = bundle
            pending.extend(tuple(x) for x in bundle["previous"])
        return sources

    async def read(self, context, arguments):
        if set(arguments) != {"source", "kind", "reference", "offset", "limit"}:
            return {"error": "INVALID_ARGUMENT"}
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
            return {"error": "INVALID_ARGUMENT"}
        sources = self._sources()
        if source not in sources:
            return {"error": "SOURCE_NOT_AUTHORIZED"}
        bundle = sources[source]
        if kind == "view":
            if reference != "":
                return {"error": "view reference must be empty"}
            text = json.dumps(bundle["records"], ensure_ascii=False)
        elif kind == "event":
            if reference not in bundle["raw"]:
                return {"error": "EVENT_NOT_IN_SOURCE"}
            text = json.dumps(bundle["raw"][reference], ensure_ascii=False)
        else:
            if reference not in bundle["artifacts"]:
                return {"error": "ARTIFACT_NOT_IN_SOURCE"}
            try:
                chunk = self.projector.gateway.for_session(source).read(
                    reference, offset, limit
                )
            except ArtifactOffsetOutOfRangeError:
                return {"error": "OFFSET_OUT_OF_RANGE"}
            return asdict(chunk)
        if offset > len(text):
            return {"error": "OFFSET_OUT_OF_RANGE"}
        end = min(len(text), offset + limit)
        return {
            "source": source,
            "content": text[offset:end],
            "offset": offset,
            "next_offset": end if end < len(text) else None,
            "total_chars": len(text),
        }

    async def submit(self, context, arguments):
        if not self.is_reader:
            raise ValueError("only compactor can submit handoff")
        if (
            set(arguments) != {"handoff"}
            or type(arguments["handoff"]) is not str
            or not arguments["handoff"].strip()
        ):
            raise InvalidLLMResponse(
                "invalid_handoff", "handoff must be a nonempty string"
            )
        text = arguments["handoff"]
        await self.transport("compact_complete", self.session_id, {"handoff": text})
        return {"submitted": True}


def frozen_bundle(projector, events, session_id, prefix, seed):
    visible = tuple(
        e.event_id
        for e in events
        if not (
            isinstance(e.payload, DomainFactCommitted)
            and e.payload.fact_type == CONTINUED
        )
    )
    prepared = projector.prepare(
        events, visible, session_id, "", prefix=prefix, enforce_budget=False
    )
    records = [
        {"source": session_id, "sequence": seq if seq else None, "message": message}
        for seq, message in zip(prepared.source_sequences[1:], prepared.messages[1:])
    ]
    raw = {}
    for item in _translate_visible_events(events, visible, ""):
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

    for item in records:
        message = item["message"]
        if message["role"] == "tool":
            collect(json.loads(message["content"]))
    for values in raw.values():
        for message in values:
            if message["role"] == "tool":
                collect(json.loads(message["content"]))
    previous = [] if seed is None else [[seed[1]["source"], seed[1]["bundle"]]]
    return {
        "records": records,
        "raw": raw,
        "artifacts": sorted(artifacts),
        "previous": previous,
    }


class CompactBoundary:
    """Runs only between advances; Host serializes publication with input admission."""

    def __init__(self, runtime, decision, context, config, control, transport):
        self.runtime = runtime
        self.decision = decision
        self.context = context
        self.config = config
        self.control = control
        self.transport = transport
        self.scheduler = None

    async def snapshot(self, *, cutover=True):
        sid = self.context.session_id
        events = await self.runtime.snapshot(sid)
        projection = self.runtime.projector.project(sid, events)
        state = projection.state
        safe = not state.waiting_command_ids and (
            not cutover
            or (not project_pending(events) and self.control.pending_view(sid) is None)
        )
        if not safe:
            return {"safe": False}
        bundle = frozen_bundle(
            self.context.projector, events, sid, self.context.prefix, self.context.seed
        )
        ref = save_document(self.context.projector.gateway, sid, bundle)
        consumed = {step.trigger_event_id for step in state.steps}
        pending = [
            e.event_id
            for e in events
            if e.event_id not in consumed
            and (
                isinstance(e.payload, UserMessageReceived)
                or isinstance(e.payload, DomainFactCommitted)
                and e.payload.requests_decision
            )
        ]
        frame = projection.next_decision
        view = state if frame is None else frame.state
        prompt = self.decision.prompt_for(view)
        tools = self.decision.schemas_for(view)[0]
        return {
            "safe": True,
            "position": state.journal_position,
            "bundle": ref,
            "continue": state.status is RuntimeStatus.RUNNABLE,
            "pending": pending,
            "context_limit": self.config.model_context_limit,
            "input_ratio": self.config.input_budget_ratio,
            "prompt": prompt,
            "tools": tools,
        }

    async def before_advance(self):
        sid = self.context.session_id
        events = await self.runtime.snapshot(sid)
        if self.context.is_reader or project_parent(events) is not None:
            return True
        projection = self.runtime.projector.project(sid, events)
        frame = projection.next_decision
        if frame is None and projection.state.waiting_command_ids:
            # Runtime still dispatches eligible commands. No in-flight execution moves.
            return True
        if frame is None:
            prompt = self.decision.prompt_for(projection.state)
            tools = self.decision.schemas_for(projection.state)[0]
            visible = tuple(e.event_id for e in events)
        else:
            prompt = self.decision._prompt_for(frame)
            tools, _ = self.decision._schemas(frame)
            visible = frame.state.visible_event_ids
        prepared = self.context.projector.prepare(
            events,
            self.context.visible(events, visible),
            sid,
            prompt,
            tools,
            prefix=self.context.prefix,
            enforce_budget=False,
        )
        assessment = prepared.assessment
        if projection.state.waiting_command_ids:
            if not assessment.allowed:
                # Await outcomes/authorization without issuing an oversized decision.
                await self.runtime.dispatcher.start_pending(sid)
                return False
            return True
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
        if response == "retired":
            self.scheduler.retired = True
            return False
        if response == "wait":
            return False
        if response != "continue":
            raise ValueError("invalid compact boundary response")
        if frame is not None and not assessment.allowed:
            raise ModelContextBudgetExceeded(assessment)
        return True
