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
        "active": "deepseek-v4-pro",
        "router": {
            "model_list": [
                {
                    "model_name": "deepseek-v4-pro",
                    "litellm_params": {
                        "model": "deepseek/deepseek-v4-pro",
                        "api_key": "your-api-key",
                        "reasoning_effort": "high",
                    },
                }
            ],
            "num_retries": 0,
        },
    },
    "workspace": {
        "root": "D:/work/agent",
        "full_access": True,
    },
    "runtime": {
        "model_context_limit": 200000,
        "input_budget_ratio": 0.9,
        "compact_threshold_ratio": 0.55,
        "loop_guard_repeat_threshold": 3,
    },
    "channels": {
        "telegram": {
            "bot_token": "your-bot-token",
            "allowed_chat_id": None,
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
    compact_threshold_ratio: float = 0.55
    loop_guard_repeat_threshold: int = 3


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str
    allowed_chat_id: int | None


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
    compact_threshold_ratio: float = 0.55
    loop_guard_repeat_threshold: int = 3

    def __post_init__(self):
        if not 0 < self.compact_threshold_ratio < 1:
            raise ValueError("compact_threshold_ratio must be in (0, 1)")


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
    if set(model) != {"active", "router"}:
        raise ValueError("模型配置字段必须是 active/router")
    active = model["active"]
    if type(active) is not str or not active.strip():
        raise ValueError("模型配置 model.active 不能为空")
    router = model["router"]
    if type(router) is not dict or not router:
        raise ValueError("模型配置 model.router 必须是非空映射")
    return ModelConfig(active=active.strip(), router=router)


def load_app_config(path: Path | None = None) -> AppConfig:
    data = _load_config_data(path)
    if set(data) != {"model", "workspace", "runtime", "channels"}:
        raise ValueError("配置字段必须是 model/workspace/runtime/channels")
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
        "compact_threshold_ratio",
        "loop_guard_repeat_threshold",
    }:
        raise ValueError(
            "runtime 配置字段必须是 model_context_limit/input_budget_ratio/compact_threshold_ratio/loop_guard_repeat_threshold"
        )
    model_context_limit = runtime["model_context_limit"]
    if type(model_context_limit) is not int or model_context_limit < 1:
        raise ValueError("配置 runtime.model_context_limit 必须是大于 0 的整数")
    input_budget_ratio = runtime["input_budget_ratio"]
    if type(input_budget_ratio) not in (int, float) or not 0 < input_budget_ratio < 1:
        raise ValueError("配置 runtime.input_budget_ratio 必须在 (0, 1) 范围内")

    compact_threshold_ratio = runtime["compact_threshold_ratio"]
    if (
        type(compact_threshold_ratio) not in (int, float)
        or not 0 < compact_threshold_ratio < 1
    ):
        raise ValueError("runtime.compact_threshold_ratio 必须在 (0, 1) 范围内")

    if type(runtime["loop_guard_repeat_threshold"]) is not int or runtime["loop_guard_repeat_threshold"] < 2:
        raise ValueError("runtime.loop_guard_repeat_threshold must be an integer >= 2")

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
            raise ValueError("channels.telegram 字段必须是 bot_token/allowed_chat_id")
        bot_token = telegram["bot_token"]
        if not isinstance(bot_token, str) or not bot_token.strip():
            raise ValueError("配置 channels.telegram.bot_token 不能为空")
        allowed_chat_id = telegram["allowed_chat_id"]
        if allowed_chat_id is not None and type(allowed_chat_id) is not int:
            raise ValueError("配置 channels.telegram.allowed_chat_id 必须是整数或 null")
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
            compact_threshold_ratio=float(compact_threshold_ratio),
            loop_guard_repeat_threshold=runtime["loop_guard_repeat_threshold"],
        ),
        channels=ChannelsConfig(telegram=telegram_config),
    )


def assistant_config_from_app(app: AppConfig, llm: LLMApi) -> AssistantConfig:
    return AssistantConfig(
        model_name=app.model.active,
        workspace_root=app.workspace.root,
        full_access=app.workspace.full_access,
        model_context_limit=app.runtime.model_context_limit,
        input_budget_ratio=app.runtime.input_budget_ratio,
        llm=llm,
        compact_threshold_ratio=app.runtime.compact_threshold_ratio,
        loop_guard_repeat_threshold=app.runtime.loop_guard_repeat_threshold,
    )
