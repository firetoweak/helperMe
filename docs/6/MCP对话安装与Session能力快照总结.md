# MCP 对话安装与 Session 能力快照总结

## 目标

让用户直接通过对话表达“安装某个 MCP”的意图。Agent 负责理解、补问并构造配置；用户无需编写 `/mcp upsert` JSON。安装仍然属于用户信任边界，模型不能直接修改 Registry。

## 通用 Approval 边界

Core 新增来源无关的 `ApprovalRequest`、`ApprovalResolution`、`ApprovalActionRegistry`：

```text
Agent control tool → frozen ApprovalRequest → RunStatus.BLOCKED
Channel yes/no → AgentApplication.resolve_approval
→ registered ApprovalActionHandler → resolution fact
```

控制工具必须独占工具批次。若模型把 Proposal 与其他工具放在同一批，整批返回 `CONTROL_TOOL_REQUIRES_EXCLUSIVE_BATCH`，不执行任何 handler。Approval 请求和结果属于 Conversation 事实；Session 只保存 `pending_approval_id` 索引。

Channel 只接受去除首尾空白后精确的小写 `yes` 或 `no`。等待审批时其他输入不进入模型。批准与执行是同一个 Application use case，不增加可停留的 `approved/executed` 状态；执行结果直接记录为 succeeded/failed。

## MCP Proposal

MCP Plugin 提供 `propose_mcp_install` 控制工具。Agent 通过多轮对话补齐字段后，构造以下两类配置：

- 单进程 stdio：结构化 `command + args + cwd`；拒绝 Shell 解释器和复合 Shell；
- 无 Secret 的 streamable HTTP：URL 与 timeout。

Proposal 不接受 Secret 字段，也不允许把 `model_inference` 冒充可信命令来源。Application 不解析自然语言或 Shell，只消费已经通过 ToolParameters 校验的冻结 payload。

批准后的 MCP Handler 先将配置保存为 disabled，连接测试成功后再 enable；测试失败时保留 disabled 配置和真实运行态，不自动重试。

## 失败恢复与 Agent 管理面

Toolset 目录只表达当前可执行能力，因此继续只包含 enabled Server。另由 MCP Plugin 向 Agent 暴露管理面工具：`list_mcp_servers` 返回包括 disabled 项在内的已登记配置与最近运行状态，`test_mcp_server` 使用冻结配置进行真实连接测试，`propose_mcp_recovery` 为测试可用的 disabled Server 提交恢复审批。

安装与恢复审批共用 Application 的原子 `test_and_enable` 用例：测试失败保持 disabled，测试成功才推进 revision 并启用。Console 的 `/mcp retry <id>` 复用同一用例。这样 `TOOLSET_NOT_FOUND` 只表示当前不可加载，Agent 必须先查询管理状态，不能扩大推断为 Server 从未安装或不可恢复。

## Session 能力快照

`ToolsetDescriptor` 提供通用 revision。Session 创建时保存 `SessionCapabilitySnapshot(toolset_id → revision)`，每次 Run 使用 `SnapshotToolsetProvider` 将快照与当前 Provider 目录求交集：

- 新增、启用、更新、禁用、删除或撤权：统一使旧 Session 能力快照过期；
- 后续 Toolset 加载或已加载工具调用返回 `SESSION_CAPABILITIES_STALE`；
- 控制面 `/mcp reload` 创建新 Session 并捕获最新配置，不在旧 Session 内静默替换；
- 快照不保存工具 Schema，具体 Schema 仍在 `load_toolset` 时发现并冻结到当前 Run。

快照同时保存可见 Toolset revision 与 Provider snapshot token，因此未启用配置的修改也能统一失效。这套语义与 MCP 无关，可直接复用于后续 Skill 等持久能力配置。

## 真实运行发现与修复

真实端到端 benchmark 发现 MCP SDK 的 stdio/HTTP Client context 带 AnyIO Task 所有权。原实现可能在并发工具 Task 中创建连接，却在 Application 主 Task 关闭，导致跨 Task 退出 cancel scope。第一轮修复曾把 SDK 物理连接改为单次操作内创建并关闭，虽然闭合了资源所有权，却使有状态 MCP 在每次调用后丢失 Server 状态。

真实 Playwright 使用进一步证明：Toolset 可见性可以属于 Run，但 MCP Server 的物理连接与有状态资源必须由 Application 生命周期持有。当前每个 `(server_id, revision)` 使用专属 connection owner task；SDK Client context 的创建、串行调用和关闭始终在该 Task 内完成，其他 Run/工具 Task 只通过队列提交操作。revision 变化、disable、remove、传输取消或 Application 退出时关闭 owner；跨 Run 重新加载 Toolset 不重启同 revision Server。

同一次验证还暴露出 stdio Server 未配置 `cwd` 时会继承 HelperMe 进程启动目录，导致 Playwright 的 `.playwright-mcp`、截图和临时附件写入源码仓库。当前 Composition 为 McpClientManager 注入 Agent Workspace 下的 runtime root；每个未显式配置工作目录的 Server 使用独立的 `plugins/mcp/runtime/{server_id}`。显式 `cwd` 继续原样生效，确实需要以用户任务 Workspace 为工作目录的 Server 必须主动配置，不能依赖启动目录偶然正确。

## 验证

- Core Approval、独占批次、精确确认与 Session 快照专项测试；
- MCP Proposal、Secret/Shell 拒绝、成功与失败安装路径专项测试；
- MCP Plugin 原有 Registry、Schema、连接、取消、真实 modern/legacy stdio 回归；
- `phase6b_mcp_install_benchmark.py` 使用真实 MCP stdio Server 验证：Proposal blocked → `yes` → disabled/test/enable → 旧 Session 不可见 → 新 Session 加载 → `MCP_TOOL_OK`。

最近一次报告：`tests/benchmarks/phase6b_mcp_install_last_report.json`。
