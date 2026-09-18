from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from helperme.cli.models import CliRecord
from helperme.cli.registry import CliRegistry
from helperme.tools.spec import PydanticParameters, ToolSpec


LOAD_CLI = "load_cli"


class CliRuntimeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.data = {} if data is None else data


class LoadCliInput(BaseModel):
    cli_id: str


class CliToolCatalog:
    """把已登记 CLI 投影为一个普通工具 load_cli。

    load_cli 是纯事实加载（读 registry），不 fork 进程；子命令树由模型用
    execute_command 跑 `<cli> --help` 渐进现查。
    """

    def __init__(
        self,
        registry: CliRegistry,
        *,
        max_catalog_chars: int = 20_000,
    ) -> None:
        if max_catalog_chars <= 0:
            raise ValueError("max_catalog_chars 必须大于 0")
        self.registry = registry
        self.max_catalog_chars = max_catalog_chars

    def tool_specs(self) -> list[ToolSpec]:
        records = tuple(
            sorted(self.registry.snapshot(), key=lambda item: item.name)
        )
        catalog = "\n".join(
            f"- {record.name}: {record.description}" for record in records
        )
        if len(catalog) > self.max_catalog_chars:
            raise RuntimeError("CLI_CATALOG_LIMIT: 完整 CLI 目录超出预算")
        by_id = {record.name: record for record in records}

        async def load_cli(input_data: LoadCliInput) -> dict[str, Any]:
            captured = by_id.get(input_data.cli_id)
            if captured is None:
                return _error_result(CliRuntimeError(
                    "CLI_NOT_FOUND",
                    f"CLI {input_data.cli_id} 不在当前目录中",
                    hint="从上下文中的当前 CLI 目录选择有效 ID。",
                    data={"cli_id": input_data.cli_id},
                ))
            try:
                current = await self._require_current_record(captured)
            except CliRuntimeError as exc:
                return _error_result(exc)
            return {
                "ok": True,
                "code": "CLI_LOADED",
                "data": {
                    "cli_id": current.name,
                    "revision": current.revision,
                    "description": current.description,
                    "source": current.source.to_dict(),
                    "version": current.version,
                    "resolved_path": current.resolved_path,
                    "health": (
                        None
                        if current.health is None
                        else current.health.to_dict()
                    ),
                },
                "hint": (
                    "子命令树不预生成；用 execute_command 跑 "
                    f"`{current.name} --help` 逐层现查，不要猜 flag。"
                ),
            }

        return [
            ToolSpec(
                name=LOAD_CLI,
                description=(
                    "读取一个已登记 CLI 的事实（版本、路径、体检结果）。"
                    "模型负责选择；本工具只按确定 ID 返回登记事实，不执行该 CLI。"
                    "必须单独调用，不能与依赖其结果的工具同批执行。\n"
                    "当前 CLI 目录由上下文消息提供。"
                ),
                parameters=PydanticParameters(LoadCliInput),
                handler=load_cli,
                exclusive_batch=True,
            ),
        ]

    async def _require_current_record(self, captured: CliRecord) -> CliRecord:
        current = await self.registry.get(captured.name)
        if current is None or current.revision != captured.revision:
            raise CliRuntimeError(
                "CLI_CATALOG_STALE",
                f"CLI {captured.name} 已在当前目录快照后变化",
                hint="在下一个 Step 使用最新 CLI 目录重新选择。",
                data={
                    "cli_id": captured.name,
                    "expected_revision": captured.revision,
                    "current_revision": (
                        current.revision if current is not None else None
                    ),
                },
            )
        return current


def _error_result(exc: CliRuntimeError) -> dict[str, Any]:
    return {
        "ok": False,
        "code": exc.code,
        "data": exc.data or None,
        "error": exc.message,
        "hint": exc.hint,
    }
