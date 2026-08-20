from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Mapping

from core.agent_workspace import AgentWorkspace
from plugins.mcp.models import McpServerRecord, utc_now


class McpRegistry:
    """MCP Server 安装配置的持久读写。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._path = self._root / "servers.json"
        self._lock = asyncio.Lock()

    @classmethod
    def from_agent_workspace(cls, workspace: AgentWorkspace) -> "McpRegistry":
        return cls(workspace.plugins_root / "mcp")

    @property
    def root(self) -> Path:
        return self._root

    @property
    def path(self) -> Path:
        return self._path

    async def list_servers(self) -> tuple[McpServerRecord, ...]:
        async with self._lock:
            return self._read_unlocked()

    def snapshot(self) -> tuple[McpServerRecord, ...]:
        """同步读取一次文件快照，供 Toolset 目录投影。"""
        return self._read_unlocked()

    async def get(self, server_id: str) -> McpServerRecord | None:
        async with self._lock:
            return self._index_unlocked().get(server_id)

    async def replace_all(
        self,
        records: Mapping[str, McpServerRecord],
    ) -> tuple[McpServerRecord, ...]:
        async with self._lock:
            ordered = tuple(
                sorted(records.values(), key=lambda item: item.id)
            )
            self._write_unlocked(ordered)
            return ordered

    async def upsert(self, record: McpServerRecord) -> McpServerRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.get(record.id)
            if existing is None:
                stored = record
            else:
                stored = McpServerRecord(
                    id=record.id,
                    display_name=record.display_name,
                    description=record.description,
                    transport=record.transport,
                    transport_config=record.transport_config,
                    enabled=record.enabled,
                    revision=existing.revision + 1,
                    created_at=existing.created_at,
                    updated_at=utc_now(),
                )
            index[record.id] = stored
            self._write_unlocked(
                tuple(sorted(index.values(), key=lambda item: item.id))
            )
            return stored

    async def set_enabled(
        self,
        server_id: str,
        enabled: bool,
    ) -> McpServerRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.get(server_id)
            if existing is None:
                raise KeyError(server_id)
            if existing.enabled == enabled:
                return existing
            updated = McpServerRecord(
                id=existing.id,
                display_name=existing.display_name,
                description=existing.description,
                transport=existing.transport,
                transport_config=existing.transport_config,
                enabled=enabled,
                revision=existing.revision + 1,
                created_at=existing.created_at,
                updated_at=utc_now(),
            )
            index[server_id] = updated
            self._write_unlocked(
                tuple(sorted(index.values(), key=lambda item: item.id))
            )
            return updated

    async def remove(self, server_id: str) -> McpServerRecord:
        async with self._lock:
            index = self._index_unlocked()
            existing = index.pop(server_id, None)
            if existing is None:
                raise KeyError(server_id)
            self._write_unlocked(
                tuple(sorted(index.values(), key=lambda item: item.id))
            )
            return existing

    def _index_unlocked(self) -> dict[str, McpServerRecord]:
        return {record.id: record for record in self._read_unlocked()}

    def _read_unlocked(self) -> tuple[McpServerRecord, ...]:
        if not self._path.exists():
            return ()
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        servers = payload.get("servers", payload)
        if not isinstance(servers, list):
            raise ValueError("servers.json 格式无效")
        return tuple(McpServerRecord.from_dict(item) for item in servers)

    def _write_unlocked(self, records: tuple[McpServerRecord, ...]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "servers": [record.to_dict() for record in records],
        }
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)
