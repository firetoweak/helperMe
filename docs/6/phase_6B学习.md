# Phase 6B · Toolset Progressive Loading + MCP Toolset Adapter

## 目标

按需渐进加载 Toolset，避免模型在 Run 开始时看到全部工具 Schema。先建立与具体 Plugin、领域和 MCP 无关的 Toolset 加载机制，再通过 MCP Adapter 验证外部 Toolset 的发现、选择与调用。

```text
精简 Toolset 目录
    ↓
load_toolset("weather")
    ↓
下一轮出现 weather 工具
    ↓
Run 结束，加载状态自然释放
```

## 职责边界

Core 管“如何加载”，Plugin 管“加载什么”。

### Plugin 设计在第二个实例中定型

6A 设计 Goal 时，已经形成了“Core 不引用具体 Plugin”的依赖规则，但 Plugin 当时仍容易被理解为某组附加工具。6B 引入性质不同的第二个实例 MCP 后，边界变得可验证：Plugin 是建立在 Core 公共端口之上的可选 Agent 辅助支架，不由“是否通用”或“是否包含工具”定义。

| Plugin 类型 | 领域职责 | 复用的 Core 端口 | 删除后的结果 |
| --- | --- | --- | --- |
| Goal 工作流型 Plugin | Contract、Executor/Judge 循环与跨 Run Goal 状态 | RunHost、RunInvocation、RunEvidence | Agent 失去 Goal Loop，普通 Run 不受影响 |
| MCP 外部能力型 Plugin | 安装、发现、启动和适配外部 MCP Toolset | AgentWorkspace、ToolsetProvider、ToolSpec | Agent 失去 MCP 能力，Core 渐进加载机制仍可服务其他 Plugin |

因此后续 Plugin 统一遵循这些边界：

- Plugin 可以拥有领域模型、应用服务、交互入口和持久状态；
- Plugin 只能组合 Core 公共端口，Core 不认识具体 Plugin 名称；
- Plugin 推动的 Core 改动必须能脱离该 Plugin 单独成立；
- 多个 Plugin 的装配、目录合并和冲突检查发生在 Composition，不进入 RunRuntime 的领域判断；
- Plugin 安装内容属于 Agent Workspace，不属于 HelperMe 源码仓库或用户任务 Workspace。

### 渐进加载对象

| 概念 | 唯一职责 |
| --- | --- |
| `ToolsetDescriptor` | 用稳定 ID 和简短描述帮助模型选择，不能提前暴露工具 schema |
| `ToolsetProvider` | 由 Plugin 实现，提供精简目录和选定 Toolset 的具体工具 |
| `RunInvocation` | 向单次 Run 注入一个可选 Provider |
| `ToolsetLoadingState` | 保存本 Run 已加载的 Toolset ID，Run 结束后自然销毁 |
| `RunRuntime` | 每轮依据加载状态重新组装工具列表 |
| `load_toolset` | 校验模型选择并更新本 Run 状态，不直接操纵 Runtime |

分类只是未来目录过大时可增加的只读索引，不是加载和生命周期对象。第一版目录规模尚未证明需要 `list_categories` 或 `list_toolsets`，因此直接把精简 Descriptor 目录注入运行指令，只提供 `load_toolset`。

## RunCapability 与 ToolsetProvider

两者语义不同，因此 Provider 直接进入 `RunInvocation`，不包装成 `RunCapability`：

- `RunCapability` 表示调用方已经决定本 Run 立即拥有的能力；
- `ToolsetProvider` 表示候选能力目录，由模型在 Run 中决定加载什么。

第一版一次 Invocation 只接受一个 Provider。多个 Plugin 的目录合并和 ID 冲突应在 Composition 层形成组合 Provider，不让 `RunRuntime` 管理 Plugin 列表。

## 每轮装配

`RunRuntime.run()` 创建空的 `ToolsetLoadingState`。每轮模型调用前：

1. 从本 Run 的基础 Registry 开始；
2. 注册 `load_toolset`；
3. 只向 Provider 获取已加载 Descriptor 对应的 `ToolSpec`；
4. 与当前 RuntimeMode 工具检查名称冲突；
5. 把本轮工具 schema 交给 ContextPreparation 和模型。

因此，模型在调用 `load_toolset("weather")` 的同一批次中不能调用尚未暴露的 weather 工具；这些工具从下一轮开始可见。未注入 Provider 的普通 Run 保留原有 Registry 和 ToolsExecutor 执行路径。

## 错误边界

模型传入未知 Toolset ID 属于外部输入边界，`load_toolset` 返回 `TOOLSET_NOT_FOUND`，允许模型在当前 Run 内根据目录修正。Provider 返回重复工具名、与基础工具或 RuntimeMode 工具冲突属于内部装配契约错误，保留原始异常直接失败。

## 当前实现

- `core/tool_registry.py`：已完成 MCP 接入前置回补；`ToolSpec` 通过 `ToolParameters` 同时获得模型 Schema 与运行时校验，支持 `PydanticParameters` 和原生 `JsonSchemaParameters`。
- `core/tools_runtime/progressive_toolsets.py`：Descriptor、Provider 公共端口、Run 内加载状态和 `load_toolset` 工具。
- `core/tools_runtime/run_invocation.py`：增加单次 Run 的可选 `toolset_provider`。
- `core/tools_runtime/run_runtime.py`：按轮重新装配已加载 Toolset 的工具和目录指令。
- `tests/core/test_progressive_toolsets.py`：覆盖下一轮可见、跨 Run 释放和未知 ID 修正边界。

## 当前验证

最小行为测试已经验证：

- 第一轮能看到 `load_toolset`，看不到未加载的 weather 工具；
- 加载成功后，weather 工具从下一轮出现并可执行；
- 即使复用同一个 `RunInvocation`，下一个 Run 也不会继承加载状态；
- 未知 Toolset 不会触发内部异常，也不会错误加载工具。

引入统一 AgentWorkspace 后，Core 全量自动化回归通过：262 tests passed，1 skipped（当前 Windows 环境无创建符号链接权限）；Plugin 回归 16 tests passed。

## MCP Plugin MVP

第二个 Plugin 实例已落地，验证了“Core 不认识 MCP、Plugin 组合公共端口”的边界。

| 能力 | 实现位置 |
| --- | --- |
| 持久安装 | `McpRegistry` + `McpSecretStore`（`~/.helperme/plugins/mcp/`） |
| 用户控制面 | `/mcp` Console 命令；非 Agent Tool |
| 懒连接 | `McpClientManager`（官方 SDK；Application resource） |
| 渐进暴露 | `McpToolsetProvider` → `load_toolset("mcp:{id}")` |
| Resources/Prompts | `McpContentService` 显式读取，不进 `load_toolset` |

Core 同步回补：`tool_specs` 异步化、`ToolsetLoadError`、`CompositeToolsetProvider`。

信任边界：普通工具不能安装或修改 MCP；Agent 可以在普通对话中收集信息并通过独占控制工具提交冻结 Proposal，用户输入精确的 `yes/no` 后由 Application use case 执行。启用即信任；目录零网络 I/O；运行态与 Registry 分离。

完整实现记录见 [MCP接入实现总结.md](MCP接入实现总结.md)；设计基线见 [MCP接入设计草稿.md](MCP接入设计草稿.md)。

## 下一步边界

- Skill 渐进加载已拆分到 6D：复用通用 Run 生命周期与能力快照规则，但不与 Toolset 数据模型提前合并；
- 真实 Streamable HTTP、stdio modern/legacy 与分页长列表均已验证；
- 可选：对话内只读查询已安装列表（仍禁止写 Registry）；
- OAuth、Approval、Resource 自动注入等后置能力按设计第 15 节按需开启。

MCP Adapter 必须继续直接使用 `JsonSchemaParameters`。禁止动态生成 Pydantic Model，也禁止静默改写外部 Schema。见 [ToolSpec格式回补总结.md](ToolSpec格式回补总结.md)。

## 对话安装与 Session 能力稳定性回补

对话安装不是把 Registry 写权限交给模型。Agent 只负责从用户意图中整理结构化配置、补问缺失字段并提交 `ApprovalRequest`；Proposal 不完整时仍是普通 Conversation，不建立额外 Draft 聚合。Proposal 工具必须独占批次，同批出现其他工具时整批拒绝。

用户输入精确的 `yes/no`，Channel 确定性拦截，不让模型解释授权。批准后 Application 根据冻结 payload 调用已注册的 Plugin Action Handler；MCP 安装按 `disabled → test → enable` 执行，失败配置保持 disabled，且不自动重试。

Session 创建时冻结通用能力快照。任意持久能力配置变化都会使旧 Session 快照统一过期；动态能力加载和调用明确失败，控制面 reload 创建新 Session 捕获最新配置。完整实现与 benchmark 见[《MCP 对话安装与 Session 能力快照总结》](MCP对话安装与Session能力快照总结.md)及[《动态能力跨 Run 连续性设计专题》](动态能力跨Run连续性设计专题.md)。
