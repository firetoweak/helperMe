# Channel 接入契约

Channel 把外部通信协议映射到 Assistant 的 Session 操作。它负责 Access、Conversation、Delivery、Reply route 四种 identity，不实现模型决策或 Session 推进循环，也不决定本机用哪套环境变量去找程序、跑命令。TUI / Telegram 的具体行为见[入口与授权](入口与授权.md)；进程驻留和进程身份见[多活跃会话](多活跃会话.md)。

本文仍是当前实现准绳。ACP v1 当前接了哪些方法见 [ACP 映射](ACP映射.md)。TUI / Web / ACP / Satori 平级并存的目标边界见 [Channel 协议改造](Channel协议改造.md)；TUI 保留原生入口，Satori 暂缓。

[Compact](Compact.md) 只切换同一业务 Session 内的上下文窗口；Channel 使用稳定 Session identity，不为压缩维护入口路由。

| identity | 用途 |
|---|---|
| Access | 谁可以使用入口 |
| Conversation | 稳定对话 identity，直接对应业务 Session |
| Delivery | 幂等接纳一条外部消息 |
| Reply route | 把输出送回正确会话 |

每个 Channel 实例还提供稳定 owner identity，并通过 `select(owner, session)` / `release(owner)` 声明用户当前使用的 Session。这是 Host 的瞬时驻留事实，不写入 Journal；Host 重启后由 Channel 重新选择。

凭证不是 Conversation identity。Telegram 当前使用 `bot_id + chat_id` 选择稳定 Conversation；token 只用于访问。进程重启恢复该对话的同一 Session，更换 Bot 不复用旧 Session。

## 输入

Channel 将原始文本交给一次 `accept_input()` 请求。Assistant 在同一个 Worker 内按固定优先级处理：control confirmation、Command authorization、terminal、普通用户消息。这样状态检查与动作之间没有 `view → action` 竞态，TUI 与 Telegram 使用相同行为。

所有普通文本，无论 Session 当时正在模型决策、执行 Command 还是等待输入，都单次接纳为 `UserMessageReceived`：

```text
外部消息
→ Host 解析当前 Session 并登记 Delivery 归属
→ accept_delivery(source, delivery_id)
→ UserMessageReceived
→ wake(session_id)
```

Channel 不区分 running/idle 输入，不创建 Interrupt 类型，不抢占当前 Step，也不等待一次用户消息对应的“Run”完成。接纳即时；当前 Step 使用冻结视图。后到消息是否已是可执行决策、旧 Outcome 组还要不要自己续跑，由 Runtime 归约，见 [Runtime 的“用户输入与后到消息”](Runtime.md#用户输入与后到消息)。

明确的授权 `yes/no` 由 Assistant 应用边界映射为 `CommandAuthorized` / `CommandRejected`。其他文本一律保留为用户消息，Runtime 不猜语义。

control confirmation 优先于 Command authorization。当前两类确认沿用既有进程内接纳语义，尚不承诺跨 Worker 重启的 delivery 幂等；普通 `UserMessageReceived` 的 Journal 幂等契约不变。control approval 的持久闭环是独立专题。

## 输出

Assistant 文本通过产品拥有的 `deliver` Command 到达 Channel sink。控制面审批提示由 Scheduler 在 Step 提交后的 Assistant 边界发送，不伪装成 Runtime Event。

## Session 操作

- `/new`：生成新 identity，创建 Session，并将当前 Channel owner 原子切换到它；
- `/resume <session_id>`：将当前 Channel owner 切换到已存在的 Session、完成未发布的窗口切换准备、重建 Host 投影，并按当前 State 决定是否 wake；切换失败时保留原选择；
- 不提供 `/stop`；
- `Ctrl+C` / `Ctrl+D`：退出进程，不写 Runtime Event。

Channel 关闭时释放 owner。Runtime 的 `WAITING` 不决定 Worker 是否退出；Host 在 Worker 静止后根据 owner 选择决定是否继续驻留。

一次回答结束后 Session 回到 `WAITING(user_message)`，后续文本进入该对话的同一 Session；compact 可以在两次模型决策之间切换 ContextWindow，Session、用户入口和 Reply route 不变。Session 没有绝对终态。

## 验收

1. 相同普通消息的 Delivery identity 只产生一个 Event。
2. 进程重启不改变 Conversation identity。
3. 连续输入按接纳顺序形成多个 `UserMessageReceived`。
4. 输入处理不等待模型或工具执行结束。
5. 输出始终回到对应 Reply route。
6. `/resume` 不重试 unknown Attempt，也不制造恢复事实。
