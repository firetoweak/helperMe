from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Mapping

from core.agent_workspace import AgentWorkspace


class McpSecretStore:
    """按 server_id 隔离的本地 SecretStore。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @classmethod
    def from_agent_workspace(cls, workspace: AgentWorkspace) -> "McpSecretStore":
        return cls(workspace.plugins_root / "mcp" / "secrets")

    def put_namespace(
        self,
        server_id: str,
        values: Mapping[str, str],
    ) -> dict[str, str]:
        """写入整命名空间，返回 secret_ref → 逻辑名映射中的 refs。"""
        if not server_id.strip():
            raise ValueError("server_id 不能为空")
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(server_id)
        temporary = path.with_suffix(".tmp")
        payload = {"values": dict(values)}
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._restrict_file(temporary)
        os.replace(temporary, path)
        self._restrict_file(path)
        return {name: self.ref_for(server_id, name) for name in values}

    def resolve(self, secret_ref: str) -> str:
        server_id, name = self._parse_ref(secret_ref)
        values = self._read_namespace(server_id)
        if name not in values:
            raise KeyError(secret_ref)
        return values[name]

    def resolve_many(self, refs: Mapping[str, str]) -> dict[str, str]:
        return {
            key: self.resolve(secret_ref)
            for key, secret_ref in refs.items()
        }

    def delete_namespace(self, server_id: str) -> None:
        path = self._path_for(server_id)
        if path.exists():
            path.unlink()

    def _read_namespace(self, server_id: str) -> dict[str, str]:
        path = self._path_for(server_id)
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        values = payload.get("values", {})
        if not isinstance(values, dict):
            raise ValueError(f"secret namespace 无效: {server_id}")
        return {str(key): str(value) for key, value in values.items()}

    def _path_for(self, server_id: str) -> Path:
        safe = server_id.replace("/", "_").replace("\\", "_")
        return self._root / f"{safe}.json"

    @staticmethod
    def ref_for(server_id: str, name: str) -> str:
        return f"mcp:{server_id}:{name}"

    @staticmethod
    def _parse_ref(secret_ref: str) -> tuple[str, str]:
        parts = secret_ref.split(":", 2)
        if len(parts) != 3 or parts[0] != "mcp" or not parts[1] or not parts[2]:
            raise ValueError(f"无效 secret_ref: {secret_ref}")
        return parts[1], parts[2]

    @staticmethod
    def _restrict_file(path: Path) -> None:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows 上 chmod 语义有限；尽力限制即可。
            pass
