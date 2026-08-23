"""HelperMe 应用配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from helperme.llm.api import LLMApi
from helperme.llm.config import ModelConfig


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "model_config.yaml"
CONFIG_PATH_ENV = "HELPER_MODEL_CONFIG"


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    root: Path
    full_access: bool


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    max_steps: int
    model_context_limit: int
    input_budget_ratio: float


@dataclass(frozen=True, slots=True)
class AppConfig:
    model: ModelConfig
    workspace: WorkspaceConfig
    runtime: RuntimeConfig


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    model_name: str
    workspace_root: Path
    full_access: bool
    max_steps: int
    model_context_limit: int
    input_budget_ratio: float
    llm: LLMApi


def _load_config_data(path: Path | None) -> dict:
    config_path = (
        Path(os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))
        if path is None
        else path
    )
    if not config_path.is_file():
        raise FileNotFoundError(
            f"模型配置不存在：{config_path}；请复制 "
            "model_config.example.yaml 并填写真实配置"
        )
    with config_path.open("r", encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file)
    if not isinstance(data, dict):
        raise ValueError("配置必须是映射")
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
    if set(data) != {"model", "workspace", "runtime"}:
        raise ValueError("配置字段必须是 model/workspace/runtime")
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
        "max_steps",
        "model_context_limit",
        "input_budget_ratio",
    }:
        raise ValueError(
            "runtime 配置字段必须是 max_steps/model_context_limit/"
            "input_budget_ratio"
        )
    max_steps = runtime["max_steps"]
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("配置 runtime.max_steps 必须是大于 0 的整数")
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

    return AppConfig(
        model=_parse_model_config(data),
        workspace=WorkspaceConfig(
            root=Path(workspace_root.strip()),
            full_access=full_access,
        ),
        runtime=RuntimeConfig(
            max_steps=max_steps,
            model_context_limit=model_context_limit,
            input_budget_ratio=float(input_budget_ratio),
        ),
    )


def assistant_config_from_app(app: AppConfig, llm: LLMApi) -> AssistantConfig:
    return AssistantConfig(
        model_name=app.model.name,
        workspace_root=app.workspace.root,
        full_access=app.workspace.full_access,
        max_steps=app.runtime.max_steps,
        model_context_limit=app.runtime.model_context_limit,
        input_budget_ratio=app.runtime.input_budget_ratio,
        llm=llm,
    )
