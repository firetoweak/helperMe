# 新栈 Toolset 渐进加载与 MCP 接入

> 状态：完成（2026-08-22）  
> 位置：Host / adapter；Runtime 只增加 `bind_tool`  
> 旧课：[Phase 6B](../6/phase_6B学习.md)、[MCP 接入实现总结](../6/MCP接入实现总结.md)

## 做了什么

模型开工时看不到全部外部工具。Host 只暴露目录和 `load_toolset`；点名成功后，**下一个 Step** 才出现 `mcp__{server}__{tool}`。

```text
Toolset 目录（enabled MCP Server）
  → load_toolset("mcp:server_id")
  → Host 发现工具并 runtime.bind_tool
  → 下一 Step 的 tools 列表才包含那些 Schema
```

Runtime 内核仍然不认识 MCP。`bind_tool` 只是允许 Host 在 Stream 进行中补 Binding。加载状态按 Stream 记在 Host，不写回 Journal。`/new` 会清掉该 Stream 的已加载目录。

复用现有 `plugins/mcp` 的 Registry / ClientManager / `/mcp` 控制面。对话安装走 Host `/mcp`，不经过 Turn BLOCKED。Runtime Command 授权见 [新栈主入口与 Command 授权](新栈主入口与Command授权.md)。

## 没有做

- 把全部 MCP 工具一次性塞进 Runtime 扁平表
- Skill（下一步）
- 自然语言 `yes` 安装 MCP
- 把 Toolset 加载状态做成 Journal 事实
