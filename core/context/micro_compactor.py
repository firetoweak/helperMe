from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from core.context.composition import (
    content_char_length,
    dehydrated_tool_content,
)
from core.messages import ConversationMessage


class MicroCompactor:
    """Level 1 投影：按 tool_artifacts 将 tool body 替换为可回读 stub，保留协议外壳。"""

    def dehydrate(
        self,
        records: list[ConversationMessage],
        tool_artifacts: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        messages = deepcopy([record.payload for record in records])
        if not tool_artifacts:
            return messages

        for index, record in enumerate(records):
            if record.message_id not in tool_artifacts:
                continue
            message = messages[index]
            if message.get("role") != "tool":
                raise ValueError(
                    "tool_artifacts 只能指向 tool 消息: "
                    f"{record.message_id}"
                )
            artifact_id = tool_artifacts[record.message_id]
            chars = content_char_length(message.get("content", ""))
            message["content"] = dehydrated_tool_content(chars, artifact_id)

        return messages

    # 兼容旧测试名；新语义为脱水而非墓碑折叠。
    def compact(
        self,
        records: list[ConversationMessage],
        tool_artifacts: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        return self.dehydrate(records, tool_artifacts)
