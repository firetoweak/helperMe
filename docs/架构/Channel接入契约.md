# Channel 接入契约

[Compact](Compact.md) 只切换同一业务 Session 内的上下文窗口；Channel 直接使用稳定 Session identity，不为压缩维护入口路由。

Channel 把外部通信协议映射到 Assistant 的 Session 操作。它负责 Access、Conversation、Delivery、Reply route 四种 identity，不实现模型决策或 Session 推进循环。

| identity | 用途 |
|---|---|
| Access | 谁可以使用入口 |
| Conversation | 稳定对话 identity，直接对应业务 Session |
| Delivery | 幂等接纳一条外部消息 |
| Reply route | 把输出送回正确会话 |

凭证不是 Conversation identity。Telegram 当前使用 `bot_id + chat_id` 选择稳定 Conversation；token 只用于访问。进程重启恢复该对话的同一 Session，更换 Bot 不复用旧 Session。

## 输入

所有普通文本，无论 Session 当时正在模型决策、执行 Command 还是等待输入，都单次接纳为 `UserMessageReceived`：

```text
外部消息
→ Host 解析当前 Session 并登记 Delivery 归属
→ accept_delivery(source, delivery_id)
→ UserMessageReceived
→ wake(session_id)
```

Channel 不区分 running/idle 输入，不创建 Interrupt 类型，不抢占当前 Step，也不等待一次用户消息对应的“Run”完成。接纳即时；当前 Step 使用冻结视图。后到消息是否已是可执行决策、旧 Outcome 组还要不要自己续跑，由 Runtime 归约，见 [Runtime 的“用户输入与后到消息”](Runtime.md#用户输入与后到消息)。

明确的授权 `yes/no` 由 Host 映射为 `CommandAuthorized` / `CommandRejected`。其他文本一律保留为用户消息，Runtime 不猜语义。

## 输出

Assistant 文本通过产品拥有的 `deliver` Command 到达 Channel sink。控制面审批提示由 Scheduler 在 Step 提交后的 Assistant 边界发送，不伪装成 Runtime Event。

## Session 操作

- `/new`：生成新 identity 并幂等创建 Session；
- `/resume <session_id>`：选择已存在的 Session、完成未发布的窗口切换准备、重建 Host 投影，并按当前 State 决定是否 wake；
- 不提供 `/stop`；
- `Ctrl+C` / `Ctrl+D`：退出进程，不写 Runtime Event。

普通 Channel 不请求 `finalize()`。一次回答结束后 Session 回到 `WAITING(user_message)`，后续文本进入该对话的同一 Session；compact 可以在两次模型决策之间切换 ContextWindow，Session、用户入口和 Reply route 不变。

## 验收

1. 相同 Delivery identity 只产生一个 Event。
2. 进程重启不改变 Conversation identity。
3. 连续输入按接纳顺序形成多个 `UserMessageReceived`。
4. 输入处理不等待模型或工具执行结束。
5. 输出始终回到对应 Reply route。
6. `/resume` 不重试 unknown Attempt，也不制造恢复事实。
