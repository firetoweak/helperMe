# MCP 接入实现总结

## 背景

依据 [MCP接入设计草稿.md](MCP接入设计草稿.md) 落地 Phase 6B 的 MCP Plugin MVP。实现目标是：把用户明确安装并信任的 MCP Server，适配为可持久配置、可按需加载、可安全调用的外部能力，同时保持 Core 不依赖 MCP。

设计基线未改写；本文件记录实现结果、模块映射、验证结论与相对设计的已知差距。

## 实现结论

MVP 已具备：

- `stdio` 与 Streamable HTTP 接入（官方 Python SDK `mcp`）；
- SDK v2 `Client(mode="auto")`：优先 `2026-07-28`，自动回退 Legacy initialize；
- Registry / SecretStore 持久化与 revision 失效；
- 用户控制面 `/mcp` 管理命令；
- enabled Server 进入 Toolset 目录，`load_toolset` 时懒连接并发现；
- 工具名命名空间、`JsonSchemaParameters` 原样 Schema、ToolResult 适配；
- Resources / Templates / Prompts 的显式 ContentService；
- Application 退出时关闭 ClientManager。

初版明确未做；其中对话安装与通用 Approval 已在后续回补中完成：

- 普通对话直接安装 / 修改 / 启停 / 删除 MCP；后续只允许对话生成冻结 Proposal，由 Application 在用户 `yes` 后安装；
- OAuth、官方 Registry、HTTP+SSE、MRTR、`subscriptions/listen`；
- Resources 自动注入 Context；
- 通用高风险 Tool Approval；当前只实现控制型 Proposal Approval；
- 多模态内容直接进入模型输入。

## Core 回补

为承载远程发现，做了与 MCP 无关的通用改动：

| 改动 | 说明 |
| --- | --- |
| `ToolsetProvider.tool_specs` → `async` | `load_toolset` 内 `await`；本地 Provider 可立即返回 |
| `ToolsetLoadError` | 加载失败转为模型可修正工具错误，且不写入 `loaded_specs` |
| `CompositeToolsetProvider` | Composition 层合并多 Provider，构造期检查 ID 冲突 |

删除 MCP Plugin 后，上述 Core 能力仍可独立服务其他 Provider。

## Plugin 模块映射

```text
plugins/mcp/
  models.py            McpServerRecord / RuntimeState / Transport 配置
  registry.py          ~/.helperme/plugins/mcp/servers.json
  secrets.py           按 server_id 隔离的本地 SecretStore
  client_manager.py    SDK Client 懒创建、缓存、失效、关闭
  adapter.py           工具名编码、Schema、CallToolResult 适配
  toolset_provider.py  目录只读 Registry；load 时发现
  content.py           Resources / Templates / Prompts 用例
  application.py       list / upsert / enable / remove / test
  console.py           /mcp 命令适配
  composition.py       create_mcp_plugin
```

持久与运行分离：

| 实体 | 生命周期 | 内容 |
| --- | --- | --- |
| `McpServerRecord` | Agent Workspace 持久 | identity、transport、credential_refs、enabled、revision、timestamps |
| `McpSecretStore` | 持久，独立文件 | 凭证真值；Registry 只存 ref |
| `McpServerRuntimeState` | 进程内可丢 | availability、capabilities、last_error、last_checked_at |

Client 缓存键为 `(server_id, revision)`。upsert / enable / remove / 凭证变化会失效旧连接。

## 信任与控制面

管理操作不是 Agent Tool。Console 提供：

```text
/mcp list [--runtime]
/mcp upsert <json>
/mcp enable <id>
/mcp disable <id>
/mcp remove <id>
/mcp test <id>
/mcp resources|resource-templates|prompts|read-resource|get-prompt ...
```

信任模型为 trust-on-enable：

- 用户必须显式安装并启用；
- 模型只能 `load_toolset` 已启用 Server；
- 普通工具无法改 Registry；对话 Proposal 获批后仍由 Application 控制面修改。

`console_chat` 将 `McpClientManager` 注入 Application resources，并把 `McpToolsetProvider` 放入普通 Turn 的 `TurnInvocation`。

## Toolset 与调用路径

```text
descriptors()          同步读 Registry（零网络）
load_toolset(mcp:id)   await list_tools → ToolSpec 快照
下一个 AgentStep                 模型可见 mcp__{server}__{tool}
tools/call             handler 闭包持有 (server_id, revision, 原名)
```

约束落实情况：

- Toolset ID = `mcp:` + record.id；
- `inputSchema` 原样进入 `JsonSchemaParameters`；非法 Schema 导致整个 Toolset 加载失败；
- `outputSchema` 在加载时编译，结果缺少或违反 `structuredContent` 时明确失败；
- 跨 Server 同名工具通过命名空间共存；
- Turn 内 revision 变化返回 `MCP_SERVER_CHANGED`，不让旧 Schema 调用新 Server；
- `isError` / transport / protocol / `input_required` 分别映射为明确错误码；
- Server instructions / Resource / Prompt 不升格为 system instruction。

## 验证

自动化：

- MCP Plugin：`28 passed`（`tests/plugins/test_mcp_plugin.py`）
- Core + Plugin 全量：`351 tests, 1 skipped`。

覆盖主路径：Registry/Secret 往返与失败回滚、目录不含 disabled、命名空间路由、加载失败不污染快照、revision 失效、非法输入/输出 Schema、HTTPS 约束、取消清理和配置失效竞态。

真实 stdio 集成已覆盖：

- v2 Server 协商 `2026-07-28`，Secret 正确注入子进程环境；Server 即使回显该凭据，也会在 MCP Adapter 外部输入边界递归替换为 `***`；
- Legacy Server 在 `server/discover` 不可用时回退 `2025-11-25 initialize`。
- 真实 Streamable HTTP Server 完成工具发现与调用；分页 fixture 汇总 120 个工具，并采用所有页面中最短 TTL；
- 已配置凭据在成功内容、`structuredContent`、`meta` 与错误中统一脱敏；专项扫描确认外置 Artifact 和格式化 Turn 日志均不含原值。

设计验收中的真实 Streamable HTTP、分页长列表和 Secret 不泄露到 Artifact/日志专项扫描均已覆盖。

## 使用要点

安装示例：

```text
/mcp upsert {"id":"demo","display_name":"Demo","description":"示例","transport":"stdio","transport_config":{"command":"python","args":["server.py"]},"enabled":true}
/mcp test demo
/mcp list
```

对话中：模型看到 enabled 目录后调用 `load_toolset("mcp:demo")`，下一个 AgentStep 使用 `mcp__demo__...` 工具。

依赖：`mcp>=2.0.0,<3`、`httpx2>=2.5.0,<3`，以及 Core LLM Client 直接使用的 `httpx>=0.27.0,<1`。HelperMe 使用 SDK v2 高层 `Client(mode="auto")` 完成现代协议发现与 Legacy 回退，不自研 dual-era。

## 后续

出现真实需求后再做：

- 只读“查询已安装列表”的对话能力；
- OAuth Auth Client + Token Store；
- `subscriptions/listen`、MRTR、Tasks、MCP Apps；
- Resource 自动注入 ContextPreparation；
- 更广泛的高风险 Tool Approval。

这些不得把 MCP 领域名词写进 Core，也不能让 RuntimeState / Client 成为第二套配置真相。

后续对话安装、Session 能力快照和真实 stdio benchmark 见[《MCP 对话安装与 Session 能力快照总结》](MCP对话安装与Session能力快照总结.md)。
