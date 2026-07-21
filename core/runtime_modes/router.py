from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json

from core.model_call.types import InvalidLLMResponse, LLMResponse


class RunMode(str, Enum):
    PLAIN = "plain"
    TODO = "todo"


@dataclass(frozen=True)
class RouteDecision:
    mode: RunMode
    reason: str


class InvalidRouteResponse(ValueError):
    pass


def parse_route_response(content: str) -> RouteDecision:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise InvalidRouteResponse("route response must be valid JSON") from exc

    if not isinstance(payload, dict):
        raise InvalidRouteResponse("route response must be a JSON object")
    if set(payload) != {"mode", "reason"}:
        raise InvalidRouteResponse(
            "route response must contain exactly mode and reason"
        )

    mode = payload["mode"]
    reason = payload["reason"]
    if not isinstance(mode, str):
        raise InvalidRouteResponse("route mode must be a string")
    try:
        run_mode = RunMode(mode)
    except ValueError as exc:
        raise InvalidRouteResponse(f"unknown route mode: {mode}") from exc
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidRouteResponse("route reason must be a non-empty string")

    return RouteDecision(run_mode, reason.strip())


class RuntimeModeRouter:
    system_prompt = """
你是本次 Run 的执行模式路由器。请根据完整 Conversation，判断最后一条用户消息明确要求的行动适合哪种执行模式。历史消息只用于理解指代和背景，不能让之前的执行模式延续到本次 Run。

- plain：用户在讨论、评价、解释或提出方案；询问看法、可能性、优化方向；可以直接回答；或只需少量短链路行动。即使主题技术上很复杂、之前刚完成 Todo 任务，也应选择 plain。
- todo：用户明确要求执行一个需要多个步骤、持续使用工具、修改与验证，或会根据观察调整路径的任务。

不能把用户提出的想法、建议或方向推断为授权实施。是否授权执行不明确时选择 plain；只有已明确要求执行、但不确定执行复杂度时才选择 todo。

示例：
- “你觉得这样改还有优化空间吗？” → plain
- “我觉得可以引入受限工作区保存大输出。” → plain
- “帮我实现受限工作区，并补测试验证。” → todo

模式选择仅对本次 Run 生效，后续 Run 重新判断。
mode 只能是 "plain" 或 "todo"。只返回严格 JSON，不要输出 Markdown 或其他文字，例如：
{"mode":"todo","reason":"需要多个步骤并验证结果"}
""".strip()

    def accept_response(self, response: LLMResponse) -> RouteDecision:
        try:
            if response.type != "text":
                raise InvalidRouteResponse("route response must be text")
            return parse_route_response(response.content)
        except InvalidRouteResponse as exc:
            raw_preview = repr(
                response.content if response.type == "text" else response
            )[:2000]
            raise InvalidLLMResponse(
                "invalid_runtime_mode_route",
                f"{exc}; raw_response={raw_preview}",
            ) from exc
