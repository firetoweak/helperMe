# agent 的调用核心 loop循环 
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from core.messages import Conversation
from core.llm_client import LLMClient
# 注册
import tools
from core.checkpoint import checkpoint_to_record
from core.tools_runner import RunResult, ToolsRunner

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SYSTEM_PROMPT = (
    "你是一个智能体助手，你可以帮助用户分析问题，并给出解决方案。用户可能提出模糊的请求。"
    "你需要根据用户提问和上下文，选择最合适的工具来解决问题。"
    "问题涉及到关键文件内容的话，必须读取关键实现文件，信息不足时继续调用工具。"
    "工具对于用户的提问来说是隐藏的。"
    "\n\n所有工具返回统一协议："
    "\n- ok=true 表示工具执行成功，可以使用 data 中的 path/content/matches 等结果继续任务。"
    "\n- ok=false 表示工具失败，必须根据 code/error/hint 调整下一步，不要假装成功。"
    "\n- code 是机器可读结果码；hint 是给你的修正建议。"
    "\n- 对会修改外部状态的工具，成功后必须调用对应验证工具确认结果，再最终回答。"
    "\n\n读文件：`data.truncated=true` 时必须用 `data.next_offset` 续读，禁止 patch。"
    "\n写文件：完成修改后必须调用 get_changes 查看实际改动。最终总结只能基于 get_changes 的 data 中真实出现的改动。"
    "如果计划修改了某处但 diff 中没有出现，必须说明「未完成」，不能声称已经修改。"
)

FILE_RULE = """涉及文件修改时，先形成最小文件操作计划，再开始修改。
计划只需要服务工具调用顺序，不需要长篇解释。
计划应包含：
1. 如何定位目标文件
2. 需要读取哪些目标片段
3. 使用哪种写入工具修改
4. 修改后调用 get_changes 验证

禁止无目标地泛泛浏览 workspace。
如果目标文件未知，先用 glob/grep 定位；定位后只读取与任务相关的文件和片段。
"""


def get_default_run_log_path() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return PROJECT_ROOT / f"run_{today}.log"


def _parse_json_maybe(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _preview_text(value: Any, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[省略 {len(text) - limit} 字符]"


def _compact_value(value: Any, *, depth: int = 0, limit: int = 240) -> Any:
    """保留结构和规模信息，避免日志记录大段正文。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return {
            "type": "str",
            "length": len(value),
            "preview": _preview_text(value, limit),
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "items_preview": [
                _compact_value(item, depth=depth + 1, limit=limit)
                for item in value[:3]
            ],
        }
    if isinstance(value, dict):
        if depth >= 2:
            return {
                "type": "dict",
                "keys": list(value.keys()),
            }
        return {
            key: _compact_value(item, depth=depth + 1, limit=limit)
            for key, item in value.items()
        }
    return _preview_text(value, limit)


def _message_metrics(messages: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "messages": len(messages),
        "tool_calls": sum(len(msg.get("tool_calls") or []) for msg in messages),
        "tool_results": sum(1 for msg in messages if msg.get("role") == "tool"),
    }


def _tool_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for index, msg in enumerate(messages):
        if msg.get("tool_calls"):
            for call in msg["tool_calls"]:
                call_id = call["id"]
                tool_names[call_id] = call["function"]["name"]
                events.append(
                    {
                        "message_index": index,
                        "event": "tool_call",
                        "id": call_id,
                        "name": call["function"]["name"],
                        "arguments": _compact_value(
                            _parse_json_maybe(call["function"]["arguments"]),
                            limit=180,
                        ),
                    }
                )
            continue

        if msg.get("role") == "tool":
            result = _parse_json_maybe(msg.get("content"))
            event = {
                "message_index": index,
                "event": "tool_result",
                "id": msg["tool_call_id"],
                "name": tool_names.get(msg["tool_call_id"]),
            }
            if isinstance(result, dict):
                event.update(
                    {
                        "ok": result.get("ok"),
                        "code": result.get("code"),
                        "error": _preview_text(result.get("error"), 180),
                        "hint": _preview_text(result.get("hint"), 180),
                        "data": _compact_value(result.get("data"), limit=180),
                    }
                )
            else:
                event["result"] = _compact_value(result, limit=180)
            events.append(event)
    return events


def _compact_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": checkpoint["kind"],
        "reason": checkpoint["reason"],
        "message": checkpoint["message"],
        "data": _compact_value(checkpoint.get("data"), limit=160),
    }


def _build_run_trace(
    *,
    started_at: str,
    question: str,
    answer: str,
    agent: "Agent",
) -> dict[str, Any]:
    ended_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    checkpoints = [
        checkpoint_to_record(checkpoint)
        for checkpoint in (agent.last_result.checkpoints if agent.last_result else [])
    ]
    compact_checkpoints = [
        _compact_checkpoint(checkpoint)
        for checkpoint in checkpoints
    ]
    return {
        "type": "agent_run",
        "started_at": started_at,
        "ended_at": ended_at,
        "model": agent.model,
        "question": question,
        "answer": answer,
        "status": agent.last_result.status if agent.last_result else None,
        "error": agent.last_result.error if agent.last_result else None,
        "metrics": {
            "answer_length": len(answer),
            "checkpoints": len(checkpoints),
            **_message_metrics(agent.conversation.messages),
        },
        "checkpoints": compact_checkpoints,
        "tool_events": _tool_events(agent.conversation.messages),
        "_messages": agent.conversation.messages,
    }


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.splitlines())


def _truncate_str(text: Any, limit: int) -> str:
    if text is None:
        return "<None>"
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [省略 {len(text) - limit} 字符]"


def _format_json_block(value: Any, *, limit: int = 600) -> str:
    if isinstance(value, str):
        parsed = _parse_json_maybe(value)
        value = parsed if parsed is not value else value
    if isinstance(value, str):
        return _truncate_str(value, limit)
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except TypeError:
        text = str(value)
    return _truncate_str(text, limit)


def _format_tool_result(result: Any) -> str:
    if not isinstance(result, dict):
        return _format_json_block(result, limit=400)

    lines: list[str] = []
    ok = result.get("ok")
    if ok is not None:
        lines.append(f"状态: {'成功' if ok else '失败'} (ok={ok})")
    if result.get("code"):
        lines.append(f"代码: {result['code']}")
    if result.get("error"):
        lines.append(f"错误: {result['error']}")
    if result.get("hint"):
        lines.append(f"提示: {result['hint']}")

    data = result.get("data")
    if data is not None:
        lines.append("数据:")
        lines.append(_indent(_format_json_block(data, limit=800)))

    return "\n".join(lines) if lines else _format_json_block(result, limit=400)


def _extract_prompts(messages: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    system_prompt: str | None = None
    user_question: str | None = None
    for msg in messages:
        role = msg.get("role")
        if role == "system" and system_prompt is None:
            system_prompt = msg.get("content")
        elif role == "user" and user_question is None:
            user_question = msg.get("content")
            break
    return system_prompt, user_question


def _extract_rounds(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 assistant(tool_calls) → tool* 分组，便于人类阅读多轮调用。"""
    rounds: list[dict[str, Any]] = []
    round_index = 0
    index = 0
    while index < len(messages):
        msg = messages[index]
        role = msg.get("role")

        if role == "user" and user_question_seen(messages, index):
            rounds.append({"kind": "user_nudge", "content": msg.get("content", "")})
            index += 1
            continue

        if role == "assistant" and msg.get("tool_calls"):
            round_index += 1
            calls_by_id = {
                call["id"]: call
                for call in msg["tool_calls"]
            }
            tool_pairs: list[dict[str, Any]] = []
            index += 1
            while index < len(messages) and messages[index].get("role") == "tool":
                tool_msg = messages[index]
                call_id = tool_msg.get("tool_call_id")
                call = calls_by_id.get(call_id)
                tool_pairs.append(
                    {
                        "name": call["function"]["name"] if call else None,
                        "arguments": call["function"]["arguments"] if call else None,
                        "result": _parse_json_maybe(tool_msg.get("content")),
                    }
                )
                index += 1
            rounds.append(
                {
                    "kind": "tool_round",
                    "round": round_index,
                    "tool_pairs": tool_pairs,
                }
            )
            continue

        if role == "assistant" and not msg.get("tool_calls"):
            rounds.append({"kind": "final_answer", "content": msg.get("content", "")})
            index += 1
            continue

        index += 1
    return rounds


def user_question_seen(messages: list[dict[str, Any]], index: int) -> bool:
    """跳过第一条 user（初始提问），其余 user 视为中途 nudge。"""
    for prior in messages[:index]:
        if prior.get("role") == "user":
            return True
    return False


def _format_checkpoints(checkpoints: list[dict[str, Any]]) -> str:
    if not checkpoints:
        return "  (无)"
    lines: list[str] = []
    for checkpoint in checkpoints:
        kind = checkpoint.get("kind", "?")
        reason = checkpoint.get("reason", "?")
        message = checkpoint.get("message", "")
        lines.append(f"  [{kind}/{reason}] {message}")
        data = checkpoint.get("data") or {}
        plan = data.get("plan")
        if not isinstance(plan, dict):
            continue

        steps = plan.get("steps") or []
        current_step = None
        for step in steps:
            if isinstance(step, dict) and step.get("status") == "doing":
                current_step = step
                break
        if current_step is None:
            for step in steps:
                if isinstance(step, dict) and step.get("status") == "pending":
                    current_step = step
                    break
        if current_step is None:
            continue

        step_id = current_step.get("id", "?")
        status = current_step.get("status", "?")
        text = current_step.get("text", "")
        note = current_step.get("note")
        line = f"    plan: step {step_id} [{status}] {text}"
        if note:
            line += f"（{note}）"
        lines.append(line)
    return "\n".join(lines)


def _format_run_log(
    *,
    started_at: str,
    ended_at: str,
    model: str,
    question: str,
    answer: str,
    status: str | None,
    error: str | None,
    messages: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    metrics: dict[str, int],
) -> str:
    width = 76
    sep = "=" * width
    thin = "-" * width
    lines: list[str] = [
        "",
        sep,
        f" Agent Run  |  {started_at}  →  {ended_at}",
        f" Model: {model}  |  Status: {status or 'unknown'}",
    ]
    if error:
        lines.append(f" Error: {error}")
    lines.extend([sep, ""])

    system_prompt, user_question = _extract_prompts(messages)
    lines.extend(["## Prompt", ""])
    if system_prompt:
        lines.extend(["### System", _indent(_truncate_str(system_prompt, 2000)), ""])
    lines.extend(
        [
            "### User",
            _indent(_truncate_str(user_question or question, 2000)),
            "",
            "## Execution",
            "",
        ]
    )

    rounds = _extract_rounds(messages)
    if not rounds:
        lines.append("  (无工具调用轮次)")
        lines.append("")
    else:
        for item in rounds:
            if item["kind"] == "user_nudge":
                lines.extend(
                    [
                        thin,
                        "### User (中途提示)",
                        _indent(_truncate_str(item["content"], 800)),
                        "",
                    ]
                )
                continue

            if item["kind"] == "final_answer":
                lines.extend(
                    [
                        thin,
                        "## Answer",
                        "",
                        _truncate_str(item["content"], 4000),
                        "",
                    ]
                )
                continue

            round_no = item["round"]
            pairs = item["tool_pairs"]
            lines.extend([thin, f"### Round {round_no}  ({len(pairs)} 个工具)", ""])
            for idx, pair in enumerate(pairs, start=1):
                name = pair.get("name") or "unknown"
                lines.append(f"  [{idx}] 调用  {name}")
                if pair.get("arguments") is not None:
                    lines.append("      参数:")
                    lines.append(_indent(_format_json_block(pair["arguments"], limit=500), prefix="      "))
                lines.append("      结果:")
                lines.append(_indent(_format_tool_result(pair["result"]), prefix="      "))
                lines.append("")

    if not any(item["kind"] == "final_answer" for item in rounds):
        lines.extend([thin, "## Answer", "", _truncate_str(answer, 4000), ""])

    lines.extend(
        [
            thin,
            "## Summary",
            "",
            f"  消息数: {metrics.get('messages', 0)}",
            f"  工具调用: {metrics.get('tool_calls', 0)}",
            f"  工具结果: {metrics.get('tool_results', 0)}",
            f"  检查点: {metrics.get('checkpoints', 0)}",
            f"  回答长度: {metrics.get('answer_length', len(answer))}",
            "",
            "### Checkpoints",
            _format_checkpoints(checkpoints),
            "",
            sep,
            "",
        ]
    )
    return "\n".join(lines)


def _write_run_log(trace: dict[str, Any], path: Path | None = None) -> None:
    log_path = path if path is not None else get_default_run_log_path()
    text = _format_run_log(
        started_at=trace["started_at"],
        ended_at=trace["ended_at"],
        model=trace["model"],
        question=trace["question"],
        answer=trace["answer"],
        status=trace.get("status"),
        error=trace.get("error"),
        messages=trace.get("_messages") or [],
        checkpoints=trace.get("checkpoints") or [],
        metrics=trace.get("metrics") or {},
    )
    with log_path.open("a", encoding="utf-8") as f:
        f.write(text)


class Agent:
    def __init__(self, model: str = "default"):
        self.llm_client = LLMClient()
        self.conversation = Conversation()
        self.model = model
        self.tools_runner = ToolsRunner(self.llm_client, self.model)
        self.last_result: RunResult | None = None

    def run(self, user_message: str, max_rounds: int = 20):
        self.last_result = self.tools_runner.run(self.conversation, user_message, max_rounds)
        return self.last_result.answer

if __name__ == "__main__":
    agent = Agent(model="qwen27b")
    agent.conversation.set_system_prompt(DEFAULT_SYSTEM_PROMPT+FILE_RULE)
    print("\n=== 测试 工具集合 ===")
    question = "[用户提问] 我现在agent连接模型部分，没有写连不上的报错兜底，帮我加上吧"
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    answer = agent.run(question)

    log_path = (
        Path(os.environ["HELPER_RUN_LOG_PATH"])
        if "HELPER_RUN_LOG_PATH" in os.environ
        else get_default_run_log_path()
    )
    trace = _build_run_trace(
        started_at=started_at,
        question=question,
        answer=answer,
        agent=agent,
    )
    _write_run_log(trace, log_path)

    print(answer)
    print(f"(运行日志已写入 {log_path})")
