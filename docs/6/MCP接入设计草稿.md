# Phase 6B · MCP 接入设计初稿

## 1. 目标与范围

Phase 6B 已完成与具体 Plugin 无关的 Toolset 渐进加载原型，以及 MCP 接入所需的异步执行链、`ToolParameters`、JSON Schema、取消和资源生命周期回补。

本设计只回答一件事：

> HelperMe 如何把用户明确安装并信任的 MCP Server，适配为可持久配置、可按需加载、可安全调用的外部能力，同时保持 Core 不依赖 MCP。

MVP 支持：

- `stdio` 本地 Server；
- Streamable HTTP Server；
- Tools 的发现、渐进加载和调用；
- Resources、Resource Templates、Prompts 的显式查询与读取；
- 静态 Bearer/Header 和 `stdio env` 凭证；
- 新旧 MCP 协议兼容交给官方 Python SDK。

MVP 不支持：

- OAuth 登录、刷新和授权升级；
- 官方 MCP Registry / 市场安装；
- 旧 HTTP+SSE Transport；
- 2026 Multi-Round-Trip Request（`input_required`）交互闭环；
- Tasks Extension、MCP Apps；
- 列表变更订阅；
- Resources 自动注入上下文；
- 通用 Tool Approval；
- 多模态内容直接进入模型输入。

## 2. 设计依据

- [MCP `2026-07-28` 规范](https://modelcontextprotocol.io/specification/2026-07-28)；
- [MCP `2026-07-28` 变更说明](https://blog.modelcontextprotocol.io/posts/2026-07-28/)；
- [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)；
- HelperMe 已有的 Plugin、Agent Workspace、`ToolsetProvider`、`ToolSpec` 和 Run 生命周期边界。

协议事实：

- `2026-07-28` 取消 `initialize` / `notifications/initialized` 和协议级 Session；
- 每个请求携带协议版本、Client 身份和能力；
- `server/discover` 用于发现版本和能力，但具体 Tools、Resources、Prompts 仍分别通过 list 请求取得；
- Legacy Server 仍可能依赖旧初始化和 Session；
- HelperMe 使用 SDK 的自动协商能力，不自行实现双时代协议栈或 JSON-RPC framing。

“协议无 Session”不代表 HelperMe 不需要 Client、Transport 或子进程生命周期。它只表示这些对象不是 MCP 领域状态，也不能被当作持久配置。

## 3. 核心原则

> MCP Server 是持久安装与信任单元；Toolset 是 Run 内的能力暴露单元。Registry 长期存在，Run 只消费已启用的 Server。Client/Transport 按需创建、可缓存、可失效、可重建，不进入领域模型。

具体约束：

1. **Core 不认识 MCP**：MCP 是可选 Plugin，移除后普通 Agent 和 Toolset 渐进加载仍可工作。
2. **配置与运行状态分离**：Registry 是安装真相；可用性、发现结果和最近错误都是可丢弃的运行态。
3. **目录不做网络 I/O**：Toolset 目录只读取 Registry；真正连接和发现发生在 `load_toolset`。
4. **Run 内 Schema 冻结**：Toolset 一经成功加载，本 Run 后续轮次始终使用同一份 ToolSpec 快照。
5. **不静默改写外部 Schema**：MCP `inputSchema` 原样进入 `JsonSchemaParameters`。
6. **跨 Server 名称必须消歧**：MCP 工具名只在单个 Server 内唯一，不能直接进入 HelperMe 的扁平 Tool Registry。
7. **用户控制安装与信任，模型控制已授权工具**：普通 Agent Run 无权新增、修改、启停或删除 MCP Server。
8. **外部内容默认不可信**：Server instructions、Prompt、Resource 和 ToolResult 都不能升级为 HelperMe system instruction。

## 4. 总体架构

```text
用户控制面
  MCP 管理命令 / Application API
          │
          ├── McpRegistry ─────── 安装配置、enabled、revision
          └── McpSecretStore ──── 凭证真值

Application 生命周期
  McpClientManager
          ├── 按 (server_id, revision) 懒创建 Client
          ├── 管理 stdio 子进程 / HTTP Client
          ├── 缓存与失效发现结果
          └── 关闭、取消、超时清理

Run 生命周期
  McpToolsetProvider
          ├── descriptors()             只读 Registry，同步
          └── async tool_specs(id)      连接、发现、适配、冻结
                       │
                       ▼
                ToolsetLoadingState
                       │
                       ▼
                 ToolSpec 快照
                       │
                       ▼
                  tools/call
```

职责边界：

| 组件 | 职责 | 不负责 |
| --- | --- | --- |
| `McpRegistry` | Server 配置的持久读写、revision、原子更新 | 探活、连接、凭证真值 |
| `McpSecretStore` | 保存 Server 私有凭证 | Server 配置和连接状态 |
| `McpClientManager` | SDK Client、Transport、缓存、取消和关闭 | Toolset 选择策略 |
| `McpToolsetProvider` | 把 enabled Server 暴露为 Descriptor，异步生成 ToolSpec | 持久化和用户授权 |
| `McpToolAdapter` | 工具名称、Schema、调用结果与错误适配 | 修改 MCP Schema 语义 |
| `McpContentService` | Resources、Templates、Prompts 的显式应用用例 | 自动注入模型上下文 |

## 5. Core 接缝

### 5.1 ToolsetProvider 改为异步加载

当前 Core 的 `tool_specs()` 是同步接口，无法承载远程发现。需要做一个与 MCP 无关的通用回补：

```python
class ToolsetProvider(Protocol):
    def descriptors(self) -> tuple[ToolsetDescriptor, ...]: ...

    async def tool_specs(
        self,
        toolset_id: str,
    ) -> tuple[ToolSpec, ...]: ...
```

`load_toolset` handler 已经是 async，因此可以直接 `await provider.tool_specs(...)`。本地 Provider 也实现 async，但可以立即返回。

加载语义：

```text
load_toolset(toolset_id)
  → 校验 Descriptor ID
  → await provider.tool_specs(toolset_id)
  → 成功：把 ToolSpec 快照写入本 Run 的 ToolsetLoadingState
  → 失败：返回明确错误，不写 loaded_specs
  → 下一轮才暴露成功加载的工具
```

### 5.2 多 Plugin 组合

`RunInvocation` 仍只接收一个 `ToolsetProvider`。Skill、MCP 等多个 Plugin 的目录合并与 ID 冲突检查由 Composition 形成通用 `CompositeToolsetProvider`，不让 `RunRuntime` 管理 Plugin 列表。

## 6. 持久模型

### 6.1 McpServerRecord

`McpServerRecord` 是唯一持久领域记录，位于：

```text
~/.helperme/plugins/mcp/
```

最小字段：

```text
McpServerRecord
├─ id                  用户侧稳定、唯一，不依赖 serverInfo.name
├─ display_name
├─ description         用户提供的可信目录描述
├─ transport           stdio | streamable_http
├─ transport_config
├─ credential_refs     只保存 SecretStore 引用
├─ enabled
├─ revision            每次有效配置变更递增
└─ created_at / updated_at
```

`stdio` 配置：

```text
command
args[]
cwd                    显式配置，不静默使用用户任务 Workspace
env_refs{}             环境变量名 → secret_ref
```

必须以 argv 直接启动进程，不拼接 shell 命令字符串。

Streamable HTTP 配置：

```text
url
header_refs{}          Header 名 → secret_ref
timeout_seconds
```

生产地址要求 HTTPS；仅本机开发地址允许 HTTP。认证 Header 不得跨 Origin 重定向转发。

Record 不保存：

- 明文密钥；
- SDK Client、Transport 或子进程；
- 协议 Session；
- 探活状态、能力列表、最近错误；
- “官方 / 非官方”标记；
- 远程 `serverInfo` 作为身份真相。

### 6.2 McpSecretStore

Secret 按 `server_id` 隔离保存。删除 Server 时删除其整个 Secret namespace，避免引用计数和跨 Server 共享凭证复杂度。

MVP 可以使用权限受限的本地 SecretStore；后续换成系统钥匙串时，不改变 Record schema 和上层端口。

Registry 和 SecretStore 的联合更新必须避免留下半写状态：先准备新 Secret，再原子替换 Registry；失败时回滚本次新建 Secret。Registry 文件写入采用临时文件替换，并使用进程内异步锁串行化管理操作。

## 7. 运行态与生命周期

### 7.1 McpServerRuntimeState

```text
McpServerRuntimeState
├─ status              unknown | available | unavailable
├─ negotiated_version
├─ capabilities
├─ last_error_summary
└─ last_checked_at
```

它是进程内、可丢、可重建的观测状态，不是第二套配置源。错误摘要必须脱敏。

### 7.2 状态语义

| Registry 状态 | Toolset 目录 | load_toolset 行为 |
| --- | --- | --- |
| `enabled=false` | 不出现 | `TOOLSET_NOT_FOUND` |
| `enabled=true` + runtime unknown | 出现 | 懒连接并发现 |
| `enabled=true` + 最近 available | 出现 | 使用有效缓存或重新发现 |
| `enabled=true` + 最近 unavailable | 仍出现并附最近失败摘要 | 允许重试，不把缓存错误当永久事实 |

因此，runtime unavailable 不负责过滤目录。否则进程启动后必须先探活全部 Server，会破坏懒加载并拖慢 Agent 启动。

### 7.3 ClientManager

Client 缓存键为 `(server_id, revision)`：

- 同一 revision 可以复用 Client/Transport；
- upsert、凭证变化、disable、remove 会失效并关闭旧实例；
- `stdio` 子进程异常退出后允许重建；
- Application 退出时统一关闭所有 Client、HTTP 连接和子进程；
- Run 或批次取消必须向正在进行的 MCP 请求传播取消；
- 不因某个 Server 连接失败而阻断 AgentApplication 启动。

`tools/list`、`resources/list` 等缓存优先遵守协议返回的 `ttlMs` 和 `cacheScope`。Legacy 响应没有这些字段时，MVP 可使用短期进程内缓存；显式 test、配置 revision 变化和连接错误会使缓存失效。

## 8. 用户控制面与信任边界

### 8.1 管理操作不是普通 Agent Tool

MVP 通过 Console 命令或 Application API 提供：

| 用例 | 职责 |
| --- | --- |
| `list_servers` | 读取 Registry，默认不探活 |
| `upsert_server` | 新建或更新配置与 Secret |
| `set_server_enabled` | 用户显式授予或撤销运行权限 |
| `remove_server` | 删除配置、Secret 并关闭 Client |
| `test_server` | 显式连接并返回协商版本、能力和错误摘要 |

这些是 MCP Plugin 的 Application use case，不注册为 `ToolSpec`。原因是 `stdio` 配置等价于持久化一个可执行命令入口，不能由普通 Agent Run 自主写入。

未来若 Core 建立通用 Approval / interrupt-resume 边界，自然语言可以生成一次待确认的管理请求；在此之前不增加 `McpDraft`、`confirm_*` 等 MCP 专用领域对象。

### 8.2 启用即信任

MVP 暂不做逐次 Tool Approval，采用简单的 trust-on-enable：

- 用户必须显式安装并启用 Server；
- 启用界面明确显示 command/URL、cwd、凭证名和能力风险；
- 模型只能加载 enabled Server；
- Server 未启用时不能启动子进程、发出请求或进入模型目录。

这只是 MVP 的信任边界，不等价于 MCP Server 安全。未来若出现通用高危工具审批需求，应在 Core 建立来源无关的 Approval，而不是在 MCP Plugin 内特制确认工具。

## 9. Toolset 与工具适配

### 9.1 Toolset ID

默认一个 Server 对应一个 Toolset：

```text
toolset_id = "mcp:" + McpServerRecord.id
```

Descriptor 使用 Record 中由用户提供的 `display_name` 和 `description`。远程 `serverInfo` 与 `instructions` 只作为诊断元数据，不能原样拼接进高优先级 Runtime Prompt。

### 9.2 工具名称空间

MCP 工具名只在 Server 内唯一，而 HelperMe 当前 ToolRegistry 是扁平名称空间。因此模型侧名称必须稳定编码：

```text
MCP 原名：search
Server ID：github
模型侧：mcp__github__search
协议调用：tools/call(name="search")
```

编码器必须满足：

- 使用稳定 `record.id`，不使用 `serverInfo.name`；
- 结果符合当前模型 Provider 的名称字符和长度限制；
- 发生清洗或截断时加入稳定短 hash，避免不同原名合并；
- ToolSpec handler 闭包保存原始 `(server_id, tool_name)`，不靠字符串反向猜测；
- 加载时检查与基础工具、RuntimeMode 工具、其他已加载 Toolset 的最终名称冲突，冲突直接失败。

### 9.3 输入 Schema

- MCP `inputSchema` 原样传入 `JsonSchemaParameters`；
- 不动态生成 Pydantic Model；
- 不补默认字段、不删除约束、不改写 `$ref`、nullable 或组合关键字；
- 非法 Schema 或顶层不是 object 时，该工具不能装配；
- 一个 Server 中单个非法工具不应悄悄消失。MVP 让整个 Toolset 加载显式失败，并报告工具名和原因。

`JsonSchemaParameters` 是协议真相。若合法 MCP Schema 与当前模型 Provider 的工具 Schema 子集不兼容，必须在模型编码边界显式失败，不能静默扩大或缩小可接受参数集合。出现第二个 Provider 或真实兼容需求后，再设计通用 `ToolEncoder`。

### 9.4 ToolResult 适配

MCP ToolResult 与 HelperMe 的 `ok/code/data/error/hint` 不同，必须显式适配：

| MCP 结果 | HelperMe 结果 |
| --- | --- |
| `complete` + `isError=false` | `ok=true`, `code=MCP_TOOL_OK` |
| `complete` + `isError=true` | `ok=false`, `code=MCP_TOOL_ERROR`，保留服务端可修正反馈 |
| `input_required` | `ok=false`, `code=MCP_INPUT_REQUIRED_UNSUPPORTED`，MVP 不自动回答或重试 |
| Transport 超时/断连 | `ok=false`, `code=MCP_TRANSPORT_ERROR`，更新 RuntimeState |
| 协议错误 | `ok=false`, `code=MCP_PROTOCOL_ERROR`，保留脱敏摘要 |
| Adapter 编程错误 | 原异常传播，不伪装成模型可修正错误 |
| `CancelledError` | 原样传播 |

`data` 中保留 MCP 的类型化结果：

```text
data.mcp.content
data.mcp.structured_content
data.mcp.meta
```

若工具声明 `outputSchema`，Adapter 应验证 `structuredContent`；不符合时返回明确的 `MCP_INVALID_TOOL_RESULT`，不能把违约数据当成功结果。

MVP 对内容类型的处理：

- Text、JSON：进入当前 ToolResult；
- Image、Audio、Embedded Resource：保留类型、MIME 和数据，由现有 Tool Result 上限决定是否外置 Artifact；
- Resource Link：保留 URI 和元数据，不自动读取；
- 所有结果继续经过现有 Evidence、Artifact 和 Context Budget 链路。

MVP 不要求模型直接理解图片或音频，但禁止静默丢弃这些内容。

## 10. Resources、Templates 与 Prompts

Tools 是模型控制的动作；Resources 和 Prompts 保持 Host / 用户控制语义，不进入 `load_toolset`，也不伪装成普通模型工具。

`McpContentService` 提供显式应用用例：

```text
list_resources(server_id, cursor)
list_resource_templates(server_id, cursor)
read_resource(server_id, uri)
list_prompts(server_id, cursor)
get_prompt(server_id, name, arguments)
```

Channel 可以把它们映射成 `/mcp ...` 命令或其他 UI。只有用户显式选择后，读取结果才作为普通外部内容加入 Conversation。

安全约束：

- Resource/Prompt 内容永远不成为 system message；
- 明确标记来源 Server 和 URI/Prompt name；
- 内容按不可信外部输入处理，不能根据其指令自动安装 Server、提升权限或泄露 Secret；
- 支持协议分页，不能只读取第一页；
- 尊重 `ttlMs/cacheScope`；
- `resources/read` 结果仍受单条输入上限和 Artifact 外置约束。

Resources 自动注入 `ContextPreparation` 是后续独立能力，不修改 MCP Client 和 ToolsetProvider 的职责。

## 11. Auth

MVP：

- `stdio` 只通过环境变量注入凭证；
- HTTP 支持静态 Bearer 或用户配置 Header；
- 凭证真值只存在 SecretStore；
- 日志、错误、Registry 和 ToolResult 不得包含 Secret；
- Server 提供的参数和内容不得被当作新的认证 Header 来源。

OAuth 后置。官方 MCP OAuth 涉及 Protected Resource Metadata、Authorization Server Discovery、PKCE、issuer/audience 校验、scope challenge 和 token refresh，不能只靠在 Record 中预留几个 URL/token 字段正确实现。

第二版实装 OAuth 时，新增独立 Auth Client 与 Token Store；`McpServerRecord` 只增加认证方式和稳定引用，不保存 access token / refresh token。

## 12. 错误与可观测性

错误按责任归类：

| 错误 | 行为 |
| --- | --- |
| 未知/未启用 Toolset | 模型可修正的 `TOOLSET_NOT_FOUND` |
| Server 不可达、超时、认证失败 | `load_toolset` 或 tool call 显式失败；不阻断 Application |
| MCP tool execution error | 返回模型可见错误，允许下一轮修正 |
| 非法外部 Schema、名称冲突、Adapter 契约错误 | 装配失败；不静默跳过 |
| Registry/SecretStore 写入失败 | 管理用例失败，不留下半写状态 |
| 取消 | 清理请求和子进程后保留原始取消语义 |

最小观测字段：

```text
server_id
transport
negotiated_protocol_version
operation                  discover | list_tools | call_tool | ...
duration_ms
outcome
sanitized_error_type
```

不得记录 Secret、完整认证 Header 或未经限制的大型 ToolResult。

## 13. MVP 实现顺序

1. `McpServerRecord`、Registry、SecretStore 与原子写入；
2. 用户控制面的 list/upsert/enable/remove/test 用例；
3. 官方 SDK Client 包装和 `McpClientManager` 生命周期；
4. 把通用 `ToolsetProvider.tool_specs()` 改为 async；
5. `McpToolsetProvider`：enabled Server 目录、load 时发现、Run 内冻结；
6. 工具名编码、`inputSchema`、ToolResult/Error Adapter；
7. 与现有 Evidence、Artifact、取消和同轮并发链路集成；
8. `McpContentService`：Resources、Templates、Prompts；
9. 真实兼容测试和最小 Agent Benchmark。

## 14. 验收标准

### 架构

- 删除 MCP Plugin 后，Core 无需修改且所有普通 Agent 测试通过；
- Core 新增的 async ToolsetProvider 能被非 MCP Provider 独立使用；
- Registry、Secret、RuntimeState、Client 生命周期没有交叉成为第二真相源。

### 行为

- Application 启动不连接任何 MCP Server；
- enabled Server 出现在目录，disabled Server 不出现；
- 首次 `load_toolset` 才连接和发现，成功后下一轮出现工具；
- 加载失败不污染 `loaded_specs`，后续允许重试；
- Run 内工具 Schema 稳定，跨 Run 重新加载；
- 两个 Server 都暴露 `search` 时可以同时加载和正确路由；
- Server 配置 revision 变化后旧 Client 被关闭，新 Run 使用新配置；
- MCP 失败不阻断普通 Agent 和其他 Server。

### 协议与安全

- 覆盖现代 `2026-07-28` 与至少一个 Legacy Server；
- 覆盖 stdio 与 Streamable HTTP；
- 分页获取完整 tools/resources/prompts 列表；
- `input_required` 明确失败，不挂起、不伪装成功；
- Text、structured、image/resource link、`isError` 均不丢失语义；
- 非法 Schema、输出违约、认证失败和取消都有专项测试；
- Registry、日志、Artifact 预览中不泄露 Secret；
- 普通 Agent Run 无法新增、修改、启停或删除 MCP Server。

## 15. 后续能力

出现真实需求后再分别设计：

- 通用 Tool Approval 与可恢复中断；
- MCP OAuth；
- `subscriptions/listen` 与列表缓存失效；
- Multi-Round-Trip Request；
- Tasks Extension；
- MCP Apps；
- 多模态模型输入适配；
- Resource 自动注入 ContextPreparation；
- 官方 Registry / 私有 Registry；
- 连接级并发上限、配额和隔离策略。

这些能力不得反向污染当前最小实体，也不能由 MCP Plugin 把领域名词写入 Core。

---

本文是 Phase 6B 的实现前设计基线。实现结果见 [MCP接入实现总结.md](MCP接入实现总结.md)。若实现时官方规范、Python SDK 或真实 Server 行为与本文冲突，以协议和可复现行为为准，并先回写本文再调整实现。
