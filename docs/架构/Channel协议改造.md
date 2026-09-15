# Channel 协议改造

> 状态：架构方向已确认。本轮只确定入口关系和职责边界，不展开详细设计或实施。当前接入行为见 [Channel 接入契约](Channel接入契约.md)，ACP 已接线能力见 [ACP 映射](ACP映射.md)。

## 目标

TUI、Web、ACP、Satori 四个 Channel 平级并存，共享 Assistant 应用能力，不以 ACP 统一所有入口。

| Channel | 职责 | 当前安排 |
|---|---|---|
| TUI | 自有终端交互，原生使用 Assistant 应用能力 | 保留独立入口，不迁入 ACP |
| Web | 自有网页交互，原生使用 Assistant 应用能力 | 确定架构位置，传输与交互细节待设计 |
| ACP | 适配 Obsidian 等外部编辑器与 ACP Client | 保留已有实现与边界约定 |
| Satori | 适配 IM 平台 | 保留架构方向，当前暂缓 |

```text
TUI ─────┐
Web ─────┤
ACP ─────┼── Assistant 应用操作 / 查询 ── Host / Worker ── Runtime / Journal
Satori ──┘   （Satori 暂缓）
```

平级不要求功能完全一致。TUI 与 Web 可以按产品需要呈现事件级历史、消息分叉等原生能力；ACP 的协议表达范围不构成其他入口的能力上限。这些能力在本文中只是架构动机，不代表已经实现，也不在本轮展开事件切点、分叉语义或接口设计。

## 职责边界

各 Channel 负责交互、外部协议适配、identity、投递和回复；Assistant 负责模型决策、授权应用操作、Session 装配与面向入口的查询投影；Runtime 负责事实持久化、确定性归约和执行不变量，不出现 TUI、Web、ACP 或 Satori 名词。

现有 `AssistantSessions` 是协议无关的应用端口。共享能力沿这条边界按实际需求扩展；Channel 不直接操作 Journal、实现 Runtime 推进循环或维护第二份会话事实。展示从 Journal / Assistant 事实投影，各入口自行适配。

不为四个入口增加宽泛的 Channel 基类、统一消息模型或能力协商框架。入口之间不互相转译，不要求连接生命周期与配置 Schema 相同。Web 的传输方式留待具体设计，不在此指定。

TUI 原生接入是正式路径，与 ACP 平级并存不属于兼容双轨。Satori 暂缓期间不启动 Telegram 替换，也不因目标架构尚未实施而删除当前 Telegram 入口。

## 共同约束

- Session 与 Event 仍以 Journal 为唯一事实源，前端聊天记录不承担恢复执行的职责。
- Channel 使用 Assistant 应用边界，不绕过 Workspace、授权与能力渐进加载规则。
- Host 按本机用户环境拉起 Worker，不使用外部编辑器残留的执行环境。
- 各入口只承诺自身已经实现的能力，不为其他入口保留无实际用途的适配路径。

## ACP 既有约定

以下保留 ACP 独立入口已有的版本、identity 与能力取舍，不作为 TUI / Web 的共同协议，也不构成本轮实施排期。

### 目标版本

当前目标客户端是 Obsidian Agent Client `0.12.1`。该发布版锁定 `@agentclientprotocol/sdk` `0.28.1`，初始化请求使用 SDK 常量 `PROTOCOL_VERSION = 1`，因此 HelperMe 的首个 ACP 实现以 **wire protocol v1** 为唯一目标，不按仍在演进的 v2 Prompt 生命周期实现。

Obsidian 当前的关键行为是：

- 通过 stdio 启动本地 Agent 子进程；
- `initialize` 发送 `protocolVersion: 1`、`clientInfo.name: "obsidian-agent-client"`；
- `session/new` 发送绝对 `cwd` 与空 `mcpServers`；
- `session/prompt` 一直等待 Agent 完成本轮并返回 `stopReason`，期间接收 `session/update`；
- Client 声明文件读写能力关闭、终端能力开启；HelperMe 首期不使用 Client 文件或终端能力；
- `session/load` 是插件用于恢复历史的稳定入口；`session/resume`、`session/list`、`session/fork` 由能力声明控制，不是首期依赖。

v1 把 `session/cancel` 列为 Agent 基线能力。HelperMe 将它映射为 `cancel_turn`：模型阶段由 `DecisionCancelled(trigger_event_id)` 与 `StepCommitted` 原子互斥；Command 阶段追加 `StepContinuationCancelled(step_event_id)`，让 Outcome 入账但不再自动续步。它不停止 Command，也不终止 Session。

### Identity 映射

| HelperMe identity | ACP 来源 | 规则 |
|---|---|---|
| Access | 本地 Agent 进程的启动者 | 首期只支持本地 stdio。操作系统用户跟启动者走；干活用的环境变量不是 Client 进程自带的那份，见[多活跃会话 · 进程身份](多活跃会话.md#进程身份)。`clientInfo` 只作诊断，不作授权依据 |
| Conversation | `sessionId` | `session/new` 由 HelperMe 生成并持久创建；`session/resume` 只接受已存在的 HelperMe Session |
| Delivery | 一次已接纳的 `session/prompt` | ACP 当前没有跨连接稳定的投递标识；首期 Adapter 在接纳边界生成唯一 ID，支持本地 stdio 的单次请求，不宣称崩溃后重发幂等 |
| Reply route | ACP connection + `sessionId` | 仅为瞬时路由；重连恢复后由新连接重新选择，不写成 Runtime 事实 |
| Owner | ACP connection 内的会话绑定 | 一条连接可以选择多个 Session；连接关闭时逐一 `release` |

不能使用 Prompt 文本哈希充当 Delivery identity：用户连续发送相同文本是两个真实事实。也不能把 JSON-RPC request id 宣称为跨连接稳定 identity；协议没有给出这项保证。

如果以后支持网络 ACP 或自动重试客户端，必须先取得协议级稳定投递标识，或明确增加双方协商的扩展；不能用内容去重、时间窗或进程内缓存伪造可靠性。

### 语义映射

当前已经接到代码的方法与 `session/update` 类型以 [ACP 映射](ACP映射.md) 为准。下表仍是目标取舍，不是实现清单。

ACP v1 的 Prompt 生命周期是一轮长请求：HelperMe 仍先执行 `accept_delivery → Event → wake`，但 ACP Adapter 在输入可靠接纳后不能立即结束 JSON-RPC 请求。它保持请求挂起，通过 `session/update` 发送输出与状态，直到该 Session 针对本轮工作重新静止，再返回 `stopReason: "end_turn"`。这只是协议等待与展示投影，不把“ACP Turn”写成新的 Runtime 状态。

| ACP 能力 | HelperMe 映射 | 首期结论 |
|---|---|---|
| `initialize` | 只接受并返回 `protocolVersion: 1`，只声明确实实现的能力 | 支持 |
| `session/new` | 创建 HelperMe Session，选择 ACP owner | 支持 |
| `session/prompt` | 先单次接纳输入，再等待 Session 为本轮重新静止并返回 `stopReason` | 支持；等待不能侵入 Runtime |
| `session/update` | Assistant 输出与展示投影发送给当前 reply route | 已推 `agent_message_chunk`、`usage_update`、`tool_call` / `tool_call_update`。模型正文 preview 与提交后的 `deliver` 使用同一 `output_id` / `message_id`；`deliver` 不重复正文，也不重复报工具。权限询问与 `session/load` 另做 |
| `session/request_permission` | UI 回答经 Assistant 应用边界提交 `CommandAuthorized` / `CommandRejected` | 目标支持，尚未接线；ACP 回答本身不是 Runtime 授权 |
| `cwd` | 外部 Workspace 请求 | 只接受产品配置授权的绝对目录；不能静默替换当前 Workspace，也不能用 Client 的工作目录或环境变量定义本机执行环境 |
| `mcpServers` | 客户端请求的能力配置 | 首期不接受注入；HelperMe 继续拥有 MCP Registry 与渐进加载 |
| `session/cancel` | 停止当前 ACP prompt 等待的自动决策链 | 调用 `cancel_turn`；不映射成用户消息、Session 终态或进程终止 |
| `session/load` | 选择已有 Session，并从 Journal 向 Client 重放用户与 Assistant 历史 | 完成最小新会话闭环后实现，以支持 Obsidian 恢复 |
| `session/resume` / `list` / `fork` / `close` | 需要额外应用操作或取消语义 | 首期不声明 |
| Client 文件系统 / 终端 | 与现有 Sandbox / Tools 所有权重叠 | 首期不声明 |

ACP 的展示消息是 Journal / Assistant 事实的投影，不是新事实源：

```text
Journal Event / Assistant lifecycle
                 │ project
                 ▼
          ACP session/update
                 │ render
                 ▼
          Obsidian / 其他外部 ACP Client
```

连接断开可以导致展示失败，但不能回滚已经提交的 Event、Step 或 Command。恢复历史时重新从 Journal 投影；不能依靠 ACP Client 保存的聊天记录恢复 Runtime。

## 当前范围与后续专题

当前仅修正四个 Channel 的架构关系及职责，不安排 TUI / Web 迁入 ACP，不推进 Satori 开发或 Telegram 替换，也不以完成 ACP 闭环作为其他入口的前置条件。

事件级历史回看、从历史继续运行与消息分叉留待独立专题讨论；本轮不确定其应用接口、持久结构或执行规则。Satori 的协议字段、identity 映射与平台迁移方案在恢复该专题时再设计。
