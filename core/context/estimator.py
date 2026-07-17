from __future__ import annotations

import json
from collections import deque
from math import ceil, floor
from typing import Any, Protocol

import tiktoken

from core.context.composition import (
    ROLE_KEYS,
    ContextComposition,
    ToolResultStat,
    collect_tool_call_names,
    content_char_length,
    dehydrated_tool_content,
    empty_role_counts,
    empty_role_tokens,
    parse_tool_result_meta,
)
from core.context.manager import ModelContext


class TokenEstimator(Protocol):
    def estimate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
    ) -> int:
        ...

    def breakdown(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
        *,
        input_budget_tokens: int,
    ) -> ContextComposition:
        ...

    def calibrate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
        actual_input_tokens: int,
    ) -> None:
        ...


class TiktokenTokenEstimator:
    """编码完整请求模板，并用近期真实 usage 校准模型差异。"""

    def __init__(self, window_size: int = 8) -> None:
        if window_size <= 0:
            raise ValueError("window_size 必须大于 0")
        self._encoding = tiktoken.get_encoding("o200k_base")
        self._observed_coefficients: deque[float] = deque(
            maxlen=window_size
        )

    @property
    def coefficient(self) -> float:
        return max((1.0, *self._observed_coefficients))

    def estimate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
    ) -> int:
        base_tokens = self._base_tokens(model_context, tools)
        return ceil(base_tokens * self.coefficient)

    def breakdown(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
        *,
        input_budget_tokens: int,
    ) -> ContextComposition:
        estimated_total = self.estimate(model_context, tools)
        by_role_base = empty_role_tokens()
        by_role_counts = empty_role_counts()
        tool_result_chars = 0
        tool_message_bases: list[tuple[int, dict[str, Any], int, int]] = []
        tool_names = collect_tool_call_names(model_context.messages)

        for index, message in enumerate(model_context.messages):
            role = message.get("role")
            if role not in ROLE_KEYS:
                role = "assistant"
            message_base = self._encode_json(message)
            by_role_base[role] += message_base
            by_role_counts[role] += 1
            if role == "tool":
                chars = content_char_length(message.get("content", ""))
                tool_result_chars += chars
                tool_message_bases.append(
                    (index, message, chars, message_base)
                )

        tools_schema_base = self._encode_json(tools) if tools else 0
        weights = {
            **by_role_base,
            "tools_schema": tools_schema_base,
        }
        scaled = self._scale_weights(weights, estimated_total)
        tool_role_tokens = scaled["tool"]
        tool_results = self._scale_tool_results(
            tool_message_bases,
            tool_names,
            tool_role_tokens,
        )
        dehydrated_estimate = self._dehydrated_tool_tokens(
            tool_message_bases,
            tool_role_base=by_role_base["tool"],
            tool_role_tokens=tool_role_tokens,
        )
        return ContextComposition(
            estimated_total_tokens=estimated_total,
            input_budget_tokens=input_budget_tokens,
            tools_schema_tokens=scaled["tools_schema"],
            by_role_tokens={
                role: scaled[role] for role in ROLE_KEYS
            },
            by_role_message_counts=by_role_counts,
            tool_result_chars=tool_result_chars,
            tool_results=tool_results,
            dehydrated_tool_tokens_estimate=dehydrated_estimate,
        )

    def calibrate(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
        actual_input_tokens: int,
    ) -> None:
        base_tokens = self._base_tokens(model_context, tools)
        self._observed_coefficients.append(
            actual_input_tokens / base_tokens
        )

    def _base_tokens(
        self,
        model_context: ModelContext,
        tools: list[dict[str, Any]],
    ) -> int:
        request_template = {
            "messages": model_context.messages,
            "tools": tools,
        }
        return self._encode_json(request_template)

    def _encode_json(self, value: Any) -> int:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return len(self._encoding.encode_ordinary(serialized))

    def _scale_tool_results(
        self,
        tool_message_bases: list[tuple[int, dict[str, Any], int, int]],
        tool_names: dict[str, str],
        tool_role_tokens: int,
    ) -> tuple[ToolResultStat, ...]:
        if not tool_message_bases:
            return ()
        base_weights = {
            str(index): message_base
            for index, _, _, message_base in tool_message_bases
        }
        scaled = self._scale_weights(base_weights, tool_role_tokens)
        stats: list[ToolResultStat] = []
        for index, message, chars, _ in tool_message_bases:
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str):
                call_id = None
            externalized, artifact_id = parse_tool_result_meta(
                message.get("content", "")
            )
            stats.append(
                ToolResultStat(
                    message_index=index,
                    tool_call_id=call_id,
                    tool_name=(
                        tool_names.get(call_id) if call_id else None
                    ),
                    chars=chars,
                    estimated_tokens=scaled[str(index)],
                    externalized=externalized,
                    artifact_id=artifact_id,
                )
            )
        return tuple(stats)

    def _dehydrated_tool_tokens(
        self,
        tool_message_bases: list[tuple[int, dict[str, Any], int, int]],
        *,
        tool_role_base: int,
        tool_role_tokens: int,
    ) -> int:
        """若全部 tool body 换成可回读 stub，tool role 的估算 token。"""
        if not tool_message_bases or tool_role_base <= 0:
            return 0
        dehydrated_tool_base = 0
        for _, message, chars, _ in tool_message_bases:
            call_id = message.get("tool_call_id")
            _, artifact_id = parse_tool_result_meta(
                message.get("content", "")
            )
            stub_message = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": dehydrated_tool_content(chars, artifact_id),
            }
            dehydrated_tool_base += self._encode_json(stub_message)
        return floor(
            dehydrated_tool_base * tool_role_tokens / tool_role_base
        )

    @staticmethod
    def _scale_weights(
        weights: dict[str, int],
        target_total: int,
    ) -> dict[str, int]:
        weight_sum = sum(weights.values())
        if target_total <= 0 or weight_sum <= 0:
            return {key: 0 for key in weights}

        scaled = {
            key: floor(value * target_total / weight_sum)
            for key, value in weights.items()
        }
        remainder = target_total - sum(scaled.values())
        if remainder == 0:
            return scaled

        # 把舍入误差补给权重最大的桶，保持总和 == estimated_total
        richest_key = max(weights, key=weights.get)
        scaled[richest_key] += remainder
        return scaled
