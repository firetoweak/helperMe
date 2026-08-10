"""模型服务的本地配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "model_config.yaml"
CONFIG_PATH_ENV = "HELPER_MODEL_CONFIG"


@dataclass(frozen=True)
class ModelConfig:
    name: str
    base_url: str
    api_key: str


@dataclass(frozen=True)
class AppConfig:
    model: ModelConfig
    workspace_root: Path


def _load_config_data(path: Path | None) -> dict:
    config_path = path or Path(
        os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH)
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
    model = data.get("model")
    if not isinstance(model, dict):
        raise ValueError("模型配置必须包含 model 映射")

    values = {}
    for field in ("name", "base_url", "api_key"):
        value = model.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"模型配置 model.{field} 不能为空")
        values[field] = value.strip()

    return ModelConfig(**values)


def load_model_config(path: Path | None = None) -> ModelConfig:
    return _parse_model_config(_load_config_data(path))


def load_app_config(path: Path | None = None) -> AppConfig:
    data = _load_config_data(path)
    workspace = data.get("workspace")
    if not isinstance(workspace, dict):
        raise ValueError("配置必须包含 workspace 映射")

    workspace_root = workspace.get("root")
    if not isinstance(workspace_root, str) or not workspace_root.strip():
        raise ValueError("配置 workspace.root 不能为空")

    return AppConfig(
        model=_parse_model_config(data),
        workspace_root=Path(workspace_root.strip()),
    )
