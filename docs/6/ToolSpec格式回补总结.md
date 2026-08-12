# ToolSpec 格式回补总结

## 背景

Phase 6B 接入 MCP 前发现，原 `ToolSpec` 同时承担 Pydantic Schema 导出与参数校验，导致 Core 的工具协议强绑定 Pydantic，无法直接承载外部 JSON Schema。

本次只回补通用工具参数契约，不实现 MCP Adapter、Schema 兼容转换或多模型工具编码体系。

## 核心设计

`ToolSpec` 依赖 `ToolParameters`，不再直接依赖 Pydantic Model：

```text
内置 Pydantic Model ── PydanticParameters ─┐
                                            ├─ ToolSpec
外部 JSON Schema ──── JsonSchemaParameters ┘
```

`ToolParameters` 同时负责：

- 向模型提供参数 Schema；
- 按同一份契约校验运行时参数。

这两个能力不能拆成无关联字段，否则模型看到的描述可能与执行时校验规则漂移。

当前实现包括：

- `PydanticParameters`：导出 `model_json_schema()`，校验后向现有 handler 提供 `BaseModel`；
- `JsonSchemaParameters`：保存外部 Schema 快照，以 `jsonschema` 在构造期检查 Schema，在执行期校验参数，校验成功后向 handler 提供原始 `dict`；
- `ToolArgumentsError`：只表达模型传入的参数不符合工具契约，由 `ToolsExecutor` 转换为可修正的 `VALIDATION_ERROR`。

## 错误边界

| 情况 | 行为 |
| --- | --- |
| arguments 不是合法 JSON | 返回 `INVALID_JSON` |
| arguments 顶层不是 object | 返回 `VALIDATION_ERROR` |
| Pydantic 或 JSON Schema 参数校验失败 | 返回 `VALIDATION_ERROR` |
| 非法 JSON Schema | 创建 `JsonSchemaParameters` 时保留原始异常并直接失败 |
| 重复工具名或工具名冲突 | Registry/装配期直接失败 |
| handler 或 Adapter 内部异常 | 不捕获、不包装，直接失败 |

非法 Schema 和重复工具名属于内部装配契约错误，不允许转换成模型可修正的 Tool Error。

## 外部 Schema 不变量

- 外部 JSON Schema 原样提供给模型，不补字段、不删除约束、不转换 nullable、不改写 `$ref`；
- 构造时建立深复制快照，避免外部对象后续修改造成单个 `ToolSpec` 内展示与校验漂移；
- 运行时使用从该快照构造的 Validator；
- MCP、OpenAPI、Plugin 等来源只要提供标准 JSON Schema，都应复用 `JsonSchemaParameters`，不能按来源创建 `McpParameters`、`OpenApiParameters` 等 Core 类型。

## 明确禁止

禁止在 MCP Plugin 中把外部 JSON Schema 动态转换成 Pydantic Model：

```text
外部 JSON Schema → 动态 Pydantic Model → ToolSpec  # 禁止
```

动态转换会让 Pydantic 继续成为事实上的 Core 协议，也无法可靠保留全部 JSON Schema 语义，容易造成展示 Schema 与实际校验规则不一致。

Pydantic 只允许作为内置工具的声明与校验方式：

```text
内置 Pydantic Model → PydanticParameters → ToolSpec
```

## 本次不实现但保留的后续方向

以下问题已经识别，但没有真实需求前不提前抽象：

- MCP Tool Adapter 与 ToolResult Adapter；
- MCP `tools/list_changed`、显式 reload 和按 Server 替换工具集合；
- 将 OpenAI-compatible 工具 envelope 从 `ToolSpec` 移到模型调用边界；出现第二个真实模型 Provider 后再考虑 `ToolEncoder`；
- 合法 JSON Schema 与特定模型 Provider 不兼容时的显式失败或兼容策略；任何转换都不能静默改变可接受参数集合；
- JSON Schema 远程 `$ref` 的获取与安全策略；当前不主动获取外部引用；
- 多模态 MCP ToolResult；
- Validator 编译缓存；当前每个 `JsonSchemaParameters` 只在构造时编译一次，尚无跨实例缓存需要；
- MCP 工具更新时的并发快照策略；当前 Run 已通过 Registry clone 获得稳定装配边界。

## 架构验收

移除全部 MCP 代码后，`JsonSchemaParameters` 仍然是可独立使用和测试的 Core 能力。Registry、ToolsExecutor 与 RunRuntime 只认识 `ToolSpec`，不知道工具来自 Pydantic、MCP、OpenAPI 或其他 Plugin。

