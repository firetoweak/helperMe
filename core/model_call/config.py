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


def load_model_config(path: Path | None = None) -> ModelConfig:
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

    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, dict):
        raise ValueError("模型配置必须包含 model 映射")

    values = {}
    for field in ("name", "base_url", "api_key"):
        value = model.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"模型配置 model.{field} 不能为空")
        values[field] = value.strip()

    return ModelConfig(**values)
