"""模型 Provider 连接配置。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelConfig:
    name: str
    base_url: str
    api_key: str

    def __post_init__(self) -> None:
        for field, value in (
            ("name", self.name),
            ("base_url", self.base_url),
            ("api_key", self.api_key),
        ):
            if type(value) is not str or not value:
                raise ValueError(f"{field} must be a non-empty str")
