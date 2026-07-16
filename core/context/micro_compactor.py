from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from core.messages import ConversationMessage


COMPACTED_TOOL_BATCH_CONTENT = "[历史成功工具批次已压缩]"


class MicroCompactor:
    def compact(
        self,
        records: list[ConversationMessage],
        through_message_id: str,
    ) -> list[dict[str, Any]]:
        boundary_index = next(
            (
                index
                for index, record in enumerate(records)
                if record.message_id == through_message_id
            ),
            None,
        )
        if boundary_index is None:
            raise ValueError(f"微压缩边界不存在: {through_message_id}")

        messages = deepcopy([record.payload for record in records])
        projected: list[dict[str, Any]] = []
        index = 0

        while index < len(messages):
            message = messages[index]
            calls = message.get("tool_calls")
            if (
                index > boundary_index
                or message.get("role") != "assistant"
                or not calls
            ):
                projected.append(message)
                index += 1
                continue

            result_end = index + 1
            while (
                result_end < len(messages)
                and messages[result_end].get("role") == "tool"
            ):
                result_end += 1

            results = messages[index + 1 : result_end]
            call_ids = [call["id"] for call in calls]
            result_ids = [result["tool_call_id"] for result in results]
            batch_is_complete = (
                result_ids == call_ids
                and result_end - 1 <= boundary_index
            )
            if not batch_is_complete:
                projected.append(message)
                index += 1
                continue

            batch_was_consumed = any(
                later.get("role") == "assistant"
                for later in messages[result_end : boundary_index + 1]
            )
            batch_succeeded = all(
                json.loads(result["content"])["ok"] is True
                for result in results
            )
            if not batch_was_consumed or not batch_succeeded:
                projected.append(message)
                index += 1
                continue

            projected.append(
                {
                    "role": "assistant",
                    "content": COMPACTED_TOOL_BATCH_CONTENT,
                }
            )
            index = result_end

        return projected
