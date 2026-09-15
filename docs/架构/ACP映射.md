# ACP 映射

当前实现清单：ACP v1 哪些方法已经接到 HelperMe，哪些只在协议里、我们没接线。目标取舍与 identity 规则仍见 [Channel 协议改造](Channel协议改造.md)。协议原文：[Initialization](https://agentclientprotocol.com/protocol/v1/initialization)、[Prompt Turn](https://agentclientprotocol.com/protocol/v1/prompt-turn)、[Tool Calls](https://agentclientprotocol.com/protocol/v1/tool-calls)。

锁定 **wire protocol v1**。入口是 `acp_chat.py` → `HelperMeAcpAgent`。ACP 没有 HelperMe 的 Step，也没有「步骤上下文」方法；一次 `session/prompt` 挂到静止为止，中间多个 Step 都落在这一轮里。

```text
ACP Client
    │ initialize / session/new / session/prompt / session/cancel
    ▼
HelperMeAcpAgent
    │ create / select / accept_input / cancel_turn / wait_quiescent
    ▼
AssistantSessions
    │
    └── session/update  ← preview + deliver / 用量 / 工具进度
```

## Client → Agent

| ACP | 状态 | HelperMe |
|---|---|---|
| `initialize` | 已映射 | 只接受 `protocolVersion: 1`。声明 `loadSession: false`；prompt 不接 image / audio / embedded context；`authMethods` 为空 |
| `session/new` | 已映射 | `create` + `select`。`cwd` 必须是已存在的绝对目录，且是配置 workspace（`full_access` 时可为子树）。拒绝 `additionalDirectories`、`mcpServers` |
| `session/prompt` | 已映射 | 只收文本块 → `accept_input`。请求挂起，等 `wait_quiescent` 后回 `end_turn`；取消则回 `cancelled` |
| `session/cancel` | 已映射 | `cancel_turn`。不映射成用户消息，不结束 Session，不杀进程 |

连接断开时 `close()` 对已选 Session `release`。这是进程收尾，不是协议里的 `session/close`。

## Agent → Client（`session/update`）

| `sessionUpdate` | 状态 | 内部来源 | 现在写什么 |
|---|---|---|---|
| `agent_message_chunk` | 已映射 | 模型 preview + `deliver` | 正文 delta 生成时立即发送；同一次输出共用 `message_id = message-{output_id}`。Step 提交后的 `deliver` 校验并完成该输出，不重复发送全文 |
| `usage_update` | 已映射 | `context_usage_sink` | `used` + `size`（窗口上限） |
| `tool_call` | 已映射 | 工具 `start` | `toolCallId` = `command_id`；`title` = 工具名；`kind` 按工具名归类；`status: in_progress`；`rawInput` |
| `tool_call_update` | 已映射 | 工具 `finish` / `fail` | `completed` 或 `failed`；`rawOutput` 是完整结果；`content` **只在有错误字符串时**填文本 |

这三类由 `acp_chat.py` 接到 Assistant 的 sink，不是 Runtime 事件。

## 未映射

初始化时没声明的，Client 不应调用。Agent 侧方法我们都没实现：

| ACP | 说明 |
|---|---|
| `authenticate` | 本地 stdio，不走协议鉴权 |
| `session/load` | 不从 Journal 向 Client 重放历史 |
| `session/list` / `delete` / `fork` / `resume` / `close` | 不声明 |
| `session/set_mode` / 配置项 | 无 Session 模式 |

我们也不调用这些 Client 方法：

| ACP | 说明 |
|---|---|
| `session/request_permission` | 授权仍走 Assistant 应用边界，不经 ACP UI |
| Client 读文件 / 写文件 / 终端 | 执行仍在本机 Sandbox |

这些 `session/update` 我们不发：

| `sessionUpdate` | 说明 |
|---|---|
| `user_message_chunk` | 用户原文不回放给 Client |
| `agent_thought_chunk` | 没有独立思考流；`deliver` 不改判成思考 |
| `plan` / `plan_update` / `plan_removed` | 无计划投影 |
| `available_commands_update` | 无斜杠命令表 |
| `current_mode_update` / `session_info_update` / `config_option_update` | 无对应产品面 |

`session/prompt` 里的图片、音频、嵌入资源一律 `invalid params`。

## 投影缺口（已接线，但位置不完整）

- 工具成功结果只在 `rawOutput`，不进文档要求的 `content[]`。Client 能不能折，看它怎么画 `rawOutput`。
- 模型用 `deliver` 做逐步汇报时，全部进 `agent_message_chunk`。这是聊天气泡，协议没有折叠面。
- 工具过程不要再 `deliver` 一遍；卡片已经走 `tool_call`。
