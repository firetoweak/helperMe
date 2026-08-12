from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Literal, Mapping
from urllib.parse import urlparse


_SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_server_id(server_id: str) -> str:
    if not _SERVER_ID_PATTERN.fullmatch(server_id):
        raise ValueError(
            "server id 必须匹配 "
            "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
        )
    return server_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TransportKind(str, Enum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class RuntimeAvailability(str, Enum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StdioTransportConfig:
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("stdio command 不能为空")
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "env_refs", dict(self.env_refs))


@dataclass(frozen=True)
class StreamableHttpTransportConfig:
    url: str
    header_refs: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("streamable_http url 不能为空")
        parsed = urlparse(self.url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "http":
            if host not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("非本机 HTTP MCP 地址必须使用 HTTPS")
        elif parsed.scheme != "https":
            raise ValueError("streamable_http url 必须是 http(s)")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        object.__setattr__(self, "header_refs", dict(self.header_refs))


TransportConfig = StdioTransportConfig | StreamableHttpTransportConfig


@dataclass(frozen=True)
class McpServerRecord:
    id: str
    display_name: str
    description: str
    transport: TransportKind
    transport_config: TransportConfig
    enabled: bool = False
    revision: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_server_id(self.id)
        if not self.display_name.strip():
            raise ValueError("display_name 不能为空")
        if self.revision < 1:
            raise ValueError("revision 必须大于 0")
        if self.transport is TransportKind.STDIO:
            if not isinstance(self.transport_config, StdioTransportConfig):
                raise ValueError("stdio transport_config 类型不匹配")
        elif self.transport is TransportKind.STREAMABLE_HTTP:
            if not isinstance(
                self.transport_config,
                StreamableHttpTransportConfig,
            ):
                raise ValueError("streamable_http transport_config 类型不匹配")
        else:
            raise ValueError(f"不支持的 transport: {self.transport}")

    @property
    def toolset_id(self) -> str:
        return f"mcp:{self.id}"

    @property
    def credential_refs(self) -> dict[str, str]:
        config = self.transport_config
        if isinstance(config, StdioTransportConfig):
            return dict(config.env_refs)
        return dict(config.header_refs)

    def to_dict(self) -> dict[str, Any]:
        config = self.transport_config
        if isinstance(config, StdioTransportConfig):
            transport_config = {
                "command": config.command,
                "args": list(config.args),
                "cwd": config.cwd,
                "env_refs": dict(config.env_refs),
            }
        else:
            transport_config = {
                "url": config.url,
                "header_refs": dict(config.header_refs),
                "timeout_seconds": config.timeout_seconds,
            }
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "transport": self.transport.value,
            "transport_config": transport_config,
            "enabled": self.enabled,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "McpServerRecord":
        transport = TransportKind(payload["transport"])
        raw_config = dict(payload["transport_config"])
        if transport is TransportKind.STDIO:
            transport_config: TransportConfig = StdioTransportConfig(
                command=raw_config["command"],
                args=tuple(raw_config.get("args") or ()),
                cwd=raw_config.get("cwd"),
                env_refs=dict(raw_config.get("env_refs") or {}),
            )
        else:
            transport_config = StreamableHttpTransportConfig(
                url=raw_config["url"],
                header_refs=dict(raw_config.get("header_refs") or {}),
                timeout_seconds=float(raw_config.get("timeout_seconds", 30)),
            )
        return cls(
            id=payload["id"],
            display_name=payload["display_name"],
            description=payload.get("description") or "",
            transport=transport,
            transport_config=transport_config,
            enabled=bool(payload.get("enabled", False)),
            revision=int(payload.get("revision", 1)),
            created_at=_parse_datetime(payload.get("created_at")),
            updated_at=_parse_datetime(payload.get("updated_at")),
        )


@dataclass
class McpServerRuntimeState:
    status: RuntimeAvailability = RuntimeAvailability.UNKNOWN
    negotiated_version: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    last_error_summary: str | None = None
    last_checked_at: datetime | None = None

    def mark_available(
        self,
        *,
        negotiated_version: str | None,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = RuntimeAvailability.AVAILABLE
        self.negotiated_version = negotiated_version
        self.capabilities = dict(capabilities or {})
        self.last_error_summary = None
        self.last_checked_at = utc_now()

    def mark_unavailable(self, error_summary: str) -> None:
        self.status = RuntimeAvailability.UNAVAILABLE
        self.negotiated_version = None
        self.capabilities = {}
        self.last_error_summary = sanitize_error_summary(error_summary)
        self.last_checked_at = utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "negotiated_version": self.negotiated_version,
            "capabilities": dict(self.capabilities),
            "last_error_summary": self.last_error_summary,
            "last_checked_at": (
                self.last_checked_at.isoformat()
                if self.last_checked_at is not None
                else None
            ),
        }


def sanitize_error_summary(
    message: str,
    *,
    secret_values: tuple[str, ...] = (),
    limit: int = 240,
) -> str:
    text = " ".join(str(message).split())
    for secret in sorted(
        (value for value in secret_values if value),
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "***")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return utc_now()


UpsertTransport = Literal["stdio", "streamable_http"]
