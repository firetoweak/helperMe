from __future__ import annotations

from base64 import b64decode
from binascii import Error as Base64Error
from collections.abc import Mapping

from helperme.assistant.attachments import AttachmentRejected, AttachmentStore
from helperme.assistant.toolsets import (
    LoadedTool,
    ToolsetDescriptor,
    ToolsetLoadError,
)
from helperme.assistant.tool_results import runtime_tool_result
from helperme.mcp.toolsets import ToolsetLoadError as ProviderLoadError


class McpToolsetAdapter:
    """Translate the MCP catalog to the Assistant Toolset port."""

    def __init__(self, mcp, attachments: AttachmentStore) -> None:
        self._provider = mcp.toolset_provider
        self._attachments = attachments

    def descriptors(self) -> tuple[ToolsetDescriptor, ...]:
        return tuple(
            ToolsetDescriptor(
                item.id,
                item.description,
                item.revision,
            )
            for item in self._provider.descriptors()
        )

    async def load(self, toolset_id: str) -> tuple[LoadedTool, ...]:
        try:
            specs = await self._provider.tool_specs(toolset_id)
        except ProviderLoadError as exc:
            raise ToolsetLoadError(
                exc.code,
                exc.message,
                hint=exc.hint,
                data=exc.data,
            ) from exc
        return tuple(_loaded_from_spec(spec, self._attachments) for spec in specs)


def extract_images(result: object, store: AttachmentStore) -> object:
    """把 MCP content 里的图片块换成附件引用。

    只认 MCP 结果结构，不递归猜测普通 JSON 的含义。必须早于文本外置执行，
    否则 base64 会被当成长文本写进文本 Artifact。
    """

    if type(result) is not dict:
        return result
    data = result.get("data")
    if type(data) is not dict or type(data.get("mcp")) is not dict:
        return result
    blocks: list[object] = []
    images: list[object] = []
    for block in data["mcp"]["content"]:
        if block.get("type") != "image":
            blocks.append(block)
            continue
        try:
            reference = store.save_image(
                b64decode(block["data"], validate=True),
                block["mimeType"],
            )
        except (AttachmentRejected, Base64Error) as exc:
            # 入口拒绝是可见事实，不静默丢图。
            blocks.append(
                {
                    "type": "image_rejected",
                    "mimeType": block["mimeType"],
                    "error": str(exc),
                }
            )
            continue
        blocks.append(reference.to_block())
        images.append(reference.to_block())
    if blocks == data["mcp"]["content"]:
        return result
    result = {
        **result,
        "data": {**data, "mcp": {**data["mcp"], "content": blocks}},
    }
    if images:
        result["images"] = images
    return result


def _loaded_from_spec(spec, attachments: AttachmentStore) -> LoadedTool:
    async def execute(arguments: Mapping[str, object]) -> object:
        return extract_images(
            runtime_tool_result(await spec.handler(arguments)),
            attachments,
        )

    return LoadedTool(
        name=spec.name,
        description=spec.description,
        parameters=dict(spec.parameters.schema()),
        execute=execute,
        requires_authorization=spec.requires_authorization,
    )
