"""HelperMe 应用配置。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from helperme.llm.api import LLMApi
from helperme.llm.config import ModelConfig
from helperme.paths import HelperMeHome


CONFIG_PATH_ENV = "HELPERME_CONFIG"
INITIAL_CONFIG = {
    "model": {
        "name": "your-model-name",
        "base_url": "https://your-model-endpoint.example/v1",
        "api_key": "your-api-key",
    },
    "workspace": {
        "root": "D:/work/agent",
        "full_access": True,
    },
    "runtime": {
        "model_context_limit": 200000,
        "input_budget_ratio": 0.9,
    },
    "channels": {
        "telegram": {
            "bot_token": "your-bot-token",
            "allowed_chat_id": 123456789,
        }
    },
}


class InitialConfigCreated(RuntimeError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"已创建初始配置：{path}；请填写后重新启动")


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    root: Path
    full_access: bool


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    model_context_limit: int
    input_budget_ratio: float


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str
    allowed_chat_id: int


@dataclass(frozen=True, slots=True)
class ChannelsConfig:
    telegram: TelegramConfig | None


@dataclass(frozen=True, slots=True)
class AppConfig:
    model: ModelConfig
    workspace: WorkspaceConfig
    runtime: RuntimeConfig
    channels: ChannelsConfig


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    model_name: str
    workspace_root: Path
    full_access: bool
    model_context_limit: int
    input_budget_ratio: float
    llm: LLMApi


def _create_initial_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as config_file:
        json.dump(INITIAL_CONFIG, config_file, ensure_ascii=False, indent=2)
        config_file.write("\n")


def _load_config_data(path: Path | None) -> dict:
    uses_default_path = path is None and CONFIG_PATH_ENV not in os.environ
    if path is not None:
        config_path = path
    elif CONFIG_PATH_ENV in os.environ:
        config_path = Path(os.environ[CONFIG_PATH_ENV])
    else:
        config_path = HelperMeHome.default().config_path
    if not config_path.is_file():
        if uses_default_path:
            _create_initial_config(config_path)
            raise InitialConfigCreated(config_path)
        raise FileNotFoundError(
            f"配置不存在：{config_path}；请复制 "
            "config.example.json 到该位置并填写真实配置"
        )
    with config_path.open("r", encoding="utf-8") as config_file:
        data = json.load(config_file)
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON object")
    return data


def _parse_model_config(data: dict) -> ModelConfig:
    model = data["model"]
    if not isinstance(model, dict):
        raise ValueError("模型配置必须包含 model 映射")
    if set(model) != {"name", "base_url", "api_key"}:
        raise ValueError("模型配置字段必须是 name/base_url/api_key")
    values = {}
    for field in ("name", "base_url", "api_key"):
        value = model[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"模型配置 model.{field} 不能为空")
        values[field] = value.strip()
    return ModelConfig(**values)


def load_app_config(path: Path | None = None) -> AppConfig:
    data = _load_config_data(path)
    if set(data) != {"model", "workspace", "runtime", "channels"}:
        raise ValueError(
            "配置字段必须是 model/workspace/runtime/channels"
        )
    workspace = data["workspace"]
    if not isinstance(workspace, dict):
        raise ValueError("配置必须包含 workspace 映射")
    if set(workspace) != {"root", "full_access"}:
        raise ValueError("workspace 配置字段必须是 root/full_access")
    workspace_root = workspace["root"]
    if not isinstance(workspace_root, str) or not workspace_root.strip():
        raise ValueError("配置 workspace.root 不能为空")
    full_access = workspace["full_access"]
    if type(full_access) is not bool:
        raise ValueError("配置 workspace.full_access 必须是布尔值")

    runtime = data["runtime"]
    if not isinstance(runtime, dict):
        raise ValueError("配置必须包含 runtime 映射")
    if set(runtime) != {
        "model_context_limit",
        "input_budget_ratio",
    }:
        raise ValueError(
            "runtime 配置字段必须是 model_context_limit/"
            "input_budget_ratio"
        )
    model_context_limit = runtime["model_context_limit"]
    if type(model_context_limit) is not int or model_context_limit < 1:
        raise ValueError(
            "配置 runtime.model_context_limit 必须是大于 0 的整数"
        )
    input_budget_ratio = runtime["input_budget_ratio"]
    if (
        type(input_budget_ratio) not in (int, float)
        or not 0 < input_budget_ratio < 1
    ):
        raise ValueError(
            "配置 runtime.input_budget_ratio 必须在 (0, 1) 范围内"
        )

    channels = data["channels"]
    if not isinstance(channels, dict):
        raise ValueError("配置必须包含 channels 映射")
    if not set(channels) <= {"telegram"}:
        raise ValueError("channels 配置只允许 telegram")
    telegram_config = None
    if "telegram" in channels:
        telegram = channels["telegram"]
        if not isinstance(telegram, dict):
            raise ValueError("channels.telegram 必须是映射")
        if set(telegram) != {"bot_token", "allowed_chat_id"}:
            raise ValueError(
                "channels.telegram 字段必须是 "
                "bot_token/allowed_chat_id"
            )
        bot_token = telegram["bot_token"]
        if not isinstance(bot_token, str) or not bot_token.strip():
            raise ValueError(
                "配置 channels.telegram.bot_token 不能为空"
            )
        allowed_chat_id = telegram["allowed_chat_id"]
        if type(allowed_chat_id) is not int:
            raise ValueError(
                "配置 channels.telegram.allowed_chat_id 必须是整数"
            )
        telegram_config = TelegramConfig(
            bot_token=bot_token.strip(),
            allowed_chat_id=allowed_chat_id,
        )

    return AppConfig(
        model=_parse_model_config(data),
        workspace=WorkspaceConfig(
            root=Path(workspace_root.strip()),
            full_access=full_access,
        ),
        runtime=RuntimeConfig(
            model_context_limit=model_context_limit,
            input_budget_ratio=float(input_budget_ratio),
        ),
        channels=ChannelsConfig(telegram=telegram_config),
    )


def assistant_config_from_app(app: AppConfig, llm: LLMApi) -> AssistantConfig:
    return AssistantConfig(
        model_name=app.model.name,
        workspace_root=app.workspace.root,
        full_access=app.workspace.full_access,
        model_context_limit=app.runtime.model_context_limit,
        input_budget_ratio=app.runtime.input_budget_ratio,
        llm=llm,
    )
