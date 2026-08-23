# 新栈主入口与 Command 授权

> 状态：完成（2026-08-22）  
> 位置：Host / adapter；Runtime 只消费授权事实

## 测验口径

新 Agent 是否成立，看它是不是 **Event 持久、State 归约、Step 决策、Command 副作用**。失败形态是：Turn 再次成为执行量子，加载 / 判定 / 工具绑在 Turn 生命周期上，或 `TurnRuntime` 外包一层假装 Runtime。

本轮刻意没有做的事：

- 不恢复 Goal Loop、`TurnHost`、`Session`、TodoList
- 不把 MCP / Skill 收成统一 Plugin 框架
- 不把对话安装 MCP / Skill 塞进 Runtime 工具结果

旧 `core/` 与 `--engine core` 已删除，见 [新栈删除 Core 与越界审计](新栈删除Core与越界审计.md)。

## 做了什么

```text
python console_chat.py
  → adapters.runtime_host
  → AgentRuntime.advance() 推进 Step
  → Dispatcher 执行 Command
  → SqliteJournal 持久 Event
```

- 默认入口是新栈。`import console_chat` 不再加载 `TurnRuntime` / `Session` / Goal。
- 旧 Turn 对照：`python console_chat.py --engine core`，实现隔离在 `console_core.py`。
- `drive_until_idle` 遇到 `WAITING(authorization:command_id)` 把控制权交还 Host，不自旋。`yes` / `no` 才写成授权事实；其他用户话仍是 `UserMessage`，由下一步模型判断。
- Host 控制台 `yes` / `no` 调用 `grant_command` / `reject_command`，Journal 留下 `CommandAuthorized` / `CommandRejected`。Runtime 不代替模型判断一句话是否换目标。
- 控制台 Journal 改为 `~/.helperme/runtime_streams/journal.sqlite`。测试仍用 `MemoryJournal`。
- 借用门把旧执行器里的 “current Turn” 措辞译成 Step；若工具返回旧 `ApprovalRequest`，标明这是 Host 控制面，不是 Runtime 未接线。

## 授权与安装的分界

| 事情 | 走哪条路 |
|---|---|
| 某个 Command 需要人批准才能 dispatch | Runtime `requires_authorization` → Host yes/no → `CommandAuthorized` |
| 安装 / 启用 MCP 或 Skill | Host `/mcp` `/skill`，不经过 Turn `BLOCKED` |

环境文件工具当前不默认要求授权。路径已经接上，以后给具体工具打标即可。

## 还没做

- 对话里 `yes` 安装 MCP / Skill（仍用控制面命令）
- 把 Toolset 加载状态写成 Journal 事实
