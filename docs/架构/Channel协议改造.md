# Channel 协议改造

> 状态：已决策，待实施。本文描述目标结构，不覆盖当前仍在运行的 TUI / Telegram 实现；当前行为仍以 [Channel 接入契约](Channel接入契约.md) 与 [入口与授权](入口与授权.md) 为准。

## 目标

HelperMe 对两类外部差异分别采用现成协议：

- ACP 连接 Obsidian、编辑器、Web、TUI 等 Agent Client；
- Satori 连接 QQ、Telegram、Discord、飞书等 IM 平台。

两种协议在 HelperMe 内部止于现有 Channel 应用边界，共同调用 `AssistantSessions`。它们不是上下游关系，也不互相转换。

```text
Obsidian / Web / TUI / 其他 ACP Client
                    │ ACP
                    ▼
          helperme/channels/acp
                    │
                    ├── AssistantSessions ── Host / Worker ── Runtime / Journal
                    │
        helperme/channels/satori
                    ▲
                    │ Satori
       QQ / Telegram / Discord / 飞书等
```

ACP 与 Satori 只适配外部协议、identity、投递和回复。Assistant 继续拥有模型决策、授权应用操作和 Session 装配；Runtime 不出现 ACP、Satori、Web 或 IM 名词。

## 不做

- 不建立同时包住 ACP 与 Satori 的通用协议框架；
- 不把 Satori 消息转换成 ACP Prompt；
- 不让 ACP Session 成为 Journal 之外的第二份会话状态；
- 不因 ACP 支持 `cwd`、MCP 或终端能力就绕过 HelperMe 的 Workspace、能力渐进加载与授权边界；
- 不把 ACP Client 的进程环境当成 HelperMe 在本机执行命令的环境；
- 不保留旧 TUI / Telegram 的长期兼容入口或双轨配置；
- 不同时兼容多个 ACP 大版本。实现时锁定一个经 Obsidian Client 实测的协议与 SDK 版本。

## ACP 目标版本

当前目标客户端是 Obsidian Agent Client `0.12.1`。该发布版锁定 `@agentclientprotocol/sdk` `0.28.1`，初始化请求使用 SDK 常量 `PROTOCOL_VERSION = 1`，因此 HelperMe 的首个 ACP 实现以 **wire protocol v1** 为唯一目标，不按仍在演进的 v2 Prompt 生命周期实现。

Obsidian 当前的关键行为是：

- 通过 stdio 启动本地 Agent 子进程；
- `initialize` 发送 `protocolVersion: 1`、`clientInfo.name: "obsidian-agent-client"`；
- `session/new` 发送绝对 `cwd` 与空 `mcpServers`；
- `session/prompt` 一直等待 Agent 完成本轮并返回 `stopReason`，期间接收 `session/update`；
- Client 声明文件读写能力关闭、终端能力开启；HelperMe 首期不使用 Client 文件或终端能力；
- `session/load` 是插件用于恢复历史的稳定入口；`session/resume`、`session/list`、`session/fork` 由能力声明控制，不是首期依赖。

v1 把 `session/cancel` 列为 Agent 基线能力。HelperMe 将它映射为 `cancel_turn`：模型阶段由 `DecisionCancelled(trigger_event_id)` 与 `StepCommitted` 原子互斥；Command 阶段追加 `StepContinuationCancelled(step_event_id)`，让 Outcome 入账但不再自动续步。它不停止 Command，也不终止 Session。

## 内部边界

现有 `AssistantSessions` 是协议无关的应用端口，不再为“统一 Channel”增加一层宽泛 `Channel` 基类。ACP 与 Satori 各自把外部事实翻译成现有窄操作：

```text
create(session_id)
select(owner, session_id)
accept_input(session_id, source, delivery_id, content)
authorize / reject
release(owner)
```

两种 Adapter 可以共享这些应用操作，但不共享外部消息模型、连接生命周期或配置 Schema。

## Identity 映射

### ACP

| HelperMe identity | ACP 来源 | 规则 |
|---|---|---|
| Access | 本地 Agent 进程的启动者 | 首期只支持本地 stdio。操作系统用户跟启动者走；干活用的环境变量不是 Client 进程自带的那份，见[多活跃会话 · 进程身份](多活跃会话.md#进程身份)。`clientInfo` 只作诊断，不作授权依据 |
| Conversation | `sessionId` | `session/new` 由 HelperMe 生成并持久创建；`session/resume` 只接受已存在的 HelperMe Session |
| Delivery | 一次已接纳的 `session/prompt` | ACP 当前没有跨连接稳定的投递标识；首期 Adapter 在接纳边界生成唯一 ID，支持本地 stdio 的单次请求，不宣称崩溃后重发幂等 |
| Reply route | ACP connection + `sessionId` | 仅为瞬时路由；重连恢复后由新连接重新选择，不写成 Runtime 事实 |
| Owner | ACP connection 内的会话绑定 | 一条连接可以选择多个 Session；连接关闭时逐一 `release` |

不能使用 Prompt 文本哈希充当 Delivery identity：用户连续发送相同文本是两个真实事实。也不能把 JSON-RPC request id 宣称为跨连接稳定 identity；协议没有给出这项保证。

如果以后支持网络 ACP 或自动重试客户端，必须先取得协议级稳定投递标识，或明确增加双方协商的扩展；不能用内容去重、时间窗或进程内缓存伪造可靠性。

### Satori

| HelperMe identity | Satori 来源 | 规则 |
|---|---|---|
| Access | `platform + self_id + user.id` | 配置在入口边界精确授权用户、群组或频道 |
| Conversation | `platform + self_id + channel.id` | 选择稳定业务 Session；凭证不进入 identity |
| Delivery | Satori event/message ID | 同一外部事件只接纳一次 |
| Reply route | account + channel | 使用产生事件的账号向原频道回复 |
| Owner | Satori Channel 实例 | 实例启动时选择，关闭时释放 |

Satori 的最终字段组合以所选 SDK 的当前协议 Schema 为准；Adapter 在外部边界校验完整字段后才构造内部 identity，不以显示名或可变昵称代替稳定 ID。

## ACP 语义映射

当前已经接到代码的方法与 `session/update` 类型以 [ACP 映射](ACP映射.md) 为准。下表仍是目标取舍，不是实现清单。

ACP v1 的 Prompt 生命周期是一轮长请求：HelperMe 仍先执行 `accept_delivery → Event → wake`，但 ACP Adapter 在输入可靠接纳后不能立即结束 JSON-RPC 请求。它保持请求挂起，通过 `session/update` 发送输出与状态，直到该 Session 针对本轮工作重新静止，再返回 `stopReason: "end_turn"`。这只是协议等待与展示投影，不把“ACP Turn”写成新的 Runtime 状态。

| ACP 能力 | HelperMe 映射 | 首期结论 |
|---|---|---|
| `initialize` | 只接受并返回 `protocolVersion: 1`，只声明确实实现的能力 | 支持 |
| `session/new` | 创建 HelperMe Session，选择 ACP owner | 支持 |
| `session/prompt` | 先单次接纳输入，再等待 Session 为本轮重新静止并返回 `stopReason` | 支持；等待不能侵入 Runtime |
| `session/update` | Assistant 输出与展示投影发送给当前 reply route | 已推 `agent_message_chunk`、`usage_update`、`tool_call` / `tool_call_update`。每次 `deliver` 使用新的 `message_id`，不把整轮 Step 拼成一条。`deliver` 仍只走正文，不重复报工具。权限询问与 `session/load` 另做 |
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
          Obsidian / Web / TUI
```

连接断开可以导致展示失败，但不能回滚已经提交的 Event、Step 或 Command。恢复历史时重新从 Journal 投影；不能依靠 ACP Client 保存的聊天记录恢复 Runtime。

## Satori 语义映射

Satori Adapter 只消费明确支持的消息事件，并将文本、附件引用和来源 identity 转成一次 Channel 输入。输出通过对应 Bot/account 的消息 API发送。

平台特有事件不进入通用分支。出现真实需求时，在 Satori Adapter 内增加明确映射；不把点赞、群成员变化等所有 Satori 事件提前泛化为 Runtime Event。

当前 Telegram 专用配对和 allowlist 行为迁移到 Satori 外部边界后，删除 `helperme/channels/telegram` 及其配置、测试和启动路径，不保留 fallback。

## 实施顺序

### 1. ACP 兼容性探针

源码探针已经确定 Obsidian Agent Client `0.12.1` 使用 ACP wire protocol v1 和 SDK `0.28.1`。实现前仍用 Obsidian 启动一个最小脚本 Agent，保存真实 wire transcript，核对初始化参数、Session 方法、Prompt 生命周期、权限请求、取消以及进程关闭行为。

这一步只验证已经选定的 v1 外部协议，不接入 Runtime，也不为了 v2 或旧 SDK 保留分支。

### 2. ACP 最小闭环

实现 `initialize → session/new → session/prompt → session/update → prompt response`，证明：

- Obsidian 选择 Vault 后能建立 HelperMe Session；
- Prompt 先进入 Journal；ACP 请求保持挂起，Session 为本轮重新静止后才返回 `end_turn`；
- Agent 输出回到正确的 Obsidian Session；用量与工具进度以标准 `session/update` 推给 Client；
- 连接关闭释放 owner，但 Journal 和 Session identity 保留；
- 未预期异常原样结束对应工作，不伪造成 `end_turn`；
- Worker 按本机用户环境查找和执行命令，不沿用 Client 进程残留的环境变量。

随后验证 `session/cancel` 的真实竞态，再接入权限请求和 `session/load`。工具状态、思考流、计划、模式和模型切换只在出现产品需求时分别增加。

### 3. 替换 TUI / Web 入口

若继续保留自有 TUI 或 Web，它们作为 ACP Client 存在，不再直接依赖 Assistant。替换完成时删除旧 TUI 直连路径；在替换完成前，ACP 不算新的主入口。

### 4. Satori 替换 Telegram

先用当前真实 Telegram 场景验证 Access、Conversation、Delivery、Reply route 四项映射，再删除 Telegram 专用实现。之后新增 IM 平台只增加 Satori 侧配置或明确的平台特性映射，不修改 Assistant / Runtime。

## 验收

1. ACP Client 与 Satori Adapter 能分别驱动相同的 `AssistantSessions` 操作，而 Runtime 不 import 或持有协议名词。
2. 从编辑器拉起的 Session 与从终端拉起的 Session，看到同一套本机用户环境；Client 进程自带的 `PATH` 等不能决定能否找到 PowerShell 或用户常用命令。
3. Obsidian 中新建和恢复会话对应同一条 HelperMe Journal 生命线。
4. ACP Prompt 开始执行以前，输入已经可靠写入 Journal；v1 请求在 Session 为本轮重新静止前不返回 `end_turn`。
5. 输出只发送到当前选择该 Session 的正确 ACP route 或 Satori route。
6. ACP 权限回答必须形成现有授权事实后才允许 Dispatcher 执行 Command。
7. 不支持的 ACP 能力在初始化时不声明；未知协议输入保留原始协议错误，不降级为普通用户消息。
8. Satori 重复事件只产生一条 `UserMessageReceived`；相同文本的不同事件产生两条事实。
9. 替换完成后不存在旧 Telegram、旧 TUI 直连、ACP、Satori 同时表达同一入口的双轨路径。
10. `session/cancel` 在模型阶段消费当前 trigger，在 Command 阶段关闭所属 Step 的 Outcome 自动续步；Command、Outcome 及副作用事实保持不变，Session 可继续追加 Event。
