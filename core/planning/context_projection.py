from __future__ import annotations

from core.messages import ConversationMessage


def project_system_prompt(
    records: list[ConversationMessage],
    system_prompt: str,
) -> list[ConversationMessage]:
    first = records[0]
    return [
        ConversationMessage(
            message_id=first.message_id,
            payload={"role": "system", "content": system_prompt},
        ),
        *records[1:],
    ]
