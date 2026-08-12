"""工具参数契约、定义与注册表。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, Mapping, Protocol

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

# ---------------------------------------------------------------------------
# 参数描述与运行时校验必须来自同一个契约。
# ---------------------------------------------------------------------------


class ToolArgumentsError(Exception):
    """模型提供的工具参数不符合工具契约。"""

    def __init__(self, details: Any) -> None:
        super().__init__("tool arguments validation failed")
        self.details = details


class ToolParameters(Protocol):
    def schema(self) -> Mapping[str, Any]:
        ...

    def validate(self, payload: Any) -> Any:
        ...


@dataclass(frozen=True)
class PydanticParameters:
    input_model: type[BaseModel]

    def schema(self) -> Mapping[str, Any]:
        return self.input_model.model_json_schema()

    def validate(self, payload: Any) -> BaseModel:
        try:
            return self.input_model.model_validate(payload)
        except PydanticValidationError as exc:
            raise ToolArgumentsError(
                exc.errors(include_context=False)
            ) from exc


@dataclass(frozen=True, init=False)
class JsonSchemaParameters:
    """直接使用外部 JSON Schema，不生成 Pydantic Model 或改写语义。"""

    _schema: dict[str, Any] = field(repr=False)
    _validator: Any = field(init=False, repr=False, compare=False)

    def __init__(self, input_schema: Mapping[str, Any]) -> None:
        schema = deepcopy(dict(input_schema))
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        object.__setattr__(self, "_schema", schema)
        object.__setattr__(self, "_validator", validator_class(schema))

    def schema(self) -> Mapping[str, Any]:
        return deepcopy(self._schema)

    def validate(self, payload: Any) -> dict[str, Any]:
        try:
            self._validator.validate(payload)
        except JsonSchemaValidationError as exc:
            raise ToolArgumentsError(
                {
                    "message": exc.message,
                    "path": list(exc.absolute_path),
                    "schema_path": list(exc.absolute_schema_path),
                    "validator": exc.validator,
                }
            ) from exc
        return payload


@dataclass
class ToolSpec:
    """与工具来源和模型 Provider 无关的内部工具定义。"""

    name: str
    description: str
    parameters: ToolParameters
    handler: Callable[[Any], Awaitable[dict[str, Any]]]

    def to_openai_tool(self) -> dict[str, Any]:
        """导出当前 OpenAI-compatible 模型接口所需的工具格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters.schema()),
            },
        }


class EmptyInput(BaseModel):
    """无参工具的占位输入模型。"""


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool registration: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def get_tools(self) -> list[dict[str, Any]]:
        return [spec.to_openai_tool() for spec in self._specs.values()]

    def clone(self) -> "ToolRegistry":
        registry = ToolRegistry()
        registry._specs = self._specs.copy()
        return registry

    def select(self, names: set[str]) -> "ToolRegistry":
        unknown = names - self._specs.keys()
        if unknown:
            raise ValueError(f"unknown base tools: {sorted(unknown)}")
        registry = ToolRegistry()
        registry._specs = {
            name: spec
            for name, spec in self._specs.items()
            if name in names
        }
        return registry


BUILTIN_TOOL_REGISTRY = ToolRegistry()


def pydantic_tool_spec(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel],
    handler: Callable[[BaseModel], Awaitable[dict[str, Any]]],
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameters=PydanticParameters(input_model),
        handler=handler,
    )


def register_tool(description: str, input_model: type[BaseModel] = EmptyInput):
    """装饰器：注册工具，自动生成 TOOLS schema 和 handler 映射。"""

    def decorator(
        fn: Callable[[BaseModel], Awaitable[dict[str, Any]]],
    ) -> Callable[[BaseModel], Awaitable[dict[str, Any]]]:
        spec = pydantic_tool_spec(
            name=fn.__name__,
            description=description,
            input_model=input_model,
            handler=fn,
        )
        BUILTIN_TOOL_REGISTRY.register(spec)
        return fn

    return decorator
