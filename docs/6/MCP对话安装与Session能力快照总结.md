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

## Session 能力快照

`ToolsetDescriptor` 提供通用 revision。Session 创建时保存 `SessionCapabilitySnapshot(toolset_id → revision)`，每次 Run 使用 `SnapshotToolsetProvider` 将快照与当前 Provider 目录求交集：

- 新增、启用、更新：旧 Session 不可见，新 Session 可见；
- 禁用、删除、撤权：当前目录立即移除；
- Run 内已加载能力发生 revision 变化：现有 MCP handler 返回 `MCP_SERVER_CHANGED`；
- 快照不保存工具 Schema，具体 Schema 仍在 `load_toolset` 时发现并冻结到当前 Run。

这套语义与 MCP 无关，可直接复用于后续 Skill 等持久能力配置。

## 真实运行发现与修复

真实端到端 benchmark 发现 MCP SDK 的 stdio/HTTP Client context 带 AnyIO Task 所有权。原实现可能在并发工具 Task 中创建连接，却在 Application 主 Task 关闭，导致跨 Task 退出 cancel scope。

SDK 物理连接现改为单次操作内创建并在同一 Task 关闭；注入的非 Task-affine测试连接仍可缓存。它牺牲了真实 SDK 连接复用，但闭合了并发 Run 的资源所有权。若未来真实性能数据要求复用，应建立专属连接 owner task，而不能重新跨 Task 持有 SDK context。

## 验证

- Core Approval、独占批次、精确确认与 Session 快照专项测试；
- MCP Proposal、Secret/Shell 拒绝、成功与失败安装路径专项测试；
- MCP Plugin 原有 Registry、Schema、连接、取消、真实 modern/legacy stdio 回归；
- `phase6b_mcp_install_benchmark.py` 使用真实 MCP stdio Server 验证：Proposal blocked → `yes` → disabled/test/enable → 旧 Session 不可见 → 新 Session 加载 → `MCP_TOOL_OK`。

最近一次报告：`tests/benchmarks/phase6b_mcp_install_last_report.json`。
