# Channel 接入契约

Channel 把外部通信协议映射到 Assistant 的 Stream 操作。它负责识别来源、选择 Stream、保证投递幂等、区分普通消息与运行中打断，并把输出送回正确会话；它不实现模型决策或 Runtime 推进语义。

## 四种 identity 必须分开

| identity | 回答的问题 | 约束 |
|---|---|---|
| Access identity | 谁可以使用入口 | 只做来源授权，不决定复用哪个 Stream |
| Conversation identity | 这条消息属于哪段连续对话 | 由 Channel 类型、稳定的服务账号身份和协议会话身份共同组成 |
| Delivery identity | 这次外部投递是否已经接收 | 必须覆盖协议实际的去重作用域，不能假设消息号全局唯一 |
| Reply route | 输出应该回到哪里 | 必须绑定产生输出的会话，不能依赖进程级“当前聊天”变量 |

Conversation identity 的通用形状是：

```text
channel kind + stable service/account identity + protocol conversation identity
```

具体字段由各 Channel 在协议边界明确选择，不在 Runtime 建立统一第三方账号模型。Telegram 当前使用 `bot_id + chat_id`。以后接入微信、飞书时，也必须先明确各自协议中“服务账号”和“会话”的稳定标识，再写 Adapter。

token、secret、access key 是可轮换凭证，不是 identity。轮换同一账号的凭证不能切断对话；更换服务账号即使面对同一个用户或群，也不能误接旧 Stream。外部数字 ID 一律按不透明标识处理，不从格式猜语义。

## 生命周期

- 进程重启不等于新对话。相同 Conversation identity 应恢复同一 Stream。
- 更换服务账号不等于进程重启。新的账号 identity 应创建独立 Stream。
- `/new`、新话题或平台原生新会话是否创建 Stream，由对应 Channel 的显式产品语义决定。
- 恢复只按完整 Conversation identity 精确选择，不猜“最近一个”，也不回退查找旧 identity 格式。若确需迁移，单独执行一次性迁移，不在生产读取路径保留兼容分支。
- 恢复出未完成工作时，Channel 必须先建立可见的运行状态与打断路径，不能让用户在不知道旧任务继续执行的情况下失去控制。

## 投递与并发

- Delivery identity 至少包含外部协议真实的去重命名空间。若消息号只在某个服务账号内唯一，就必须同时带上该账号 identity。
- 入站接收与 Agent drive 并行。Channel 不能因为正在执行工具或调用模型就停止接收新消息。
- Stream 空闲时，普通文本写成 `UserMessageReceived`；Stream 正在 drive 时，新文本写成 `UserInterruptReceived`，不能仅排到单 worker 队尾当作下一个普通任务。
- 授权回复、终止命令等确定性控制输入先由 Channel 按当前状态映射；其余文本含义仍交给模型判断。
- 协议重投必须落到同一个 Delivery identity；不同服务账号中相同的消息号不得互相去重。

## 输出路由

单账号、单会话实现可以暂时只有一个 sink，但扩展到多个用户、群或话题前，输出路由必须和 Stream/Command 的来源绑定。不得用可变的全局 `current_chat`、最后一条入站消息或当前 worker 推断回复目标，否则并行会话会串消息。

## 新 Channel 最小验收

每个 Channel 至少用自动化测试证明：

1. 同一服务账号、同一会话在进程重启后恢复同一 Stream。
2. 同一账号轮换凭证不改变 Stream identity。
3. 不同服务账号面对相同外部会话 ID 时使用不同 Stream。
4. 同一投递的协议重试只写入一次；不同账号的相同消息号互不冲突。
5. drive 期间的新文本立即成为 Interrupt，入站接收不会等待旧任务结束。
6. 未授权来源不会创建 Stream、写 Journal 或触发输出。
7. 配对/绑定模式不装配任务执行面，也不接受普通任务。
8. 多会话并发时，输出始终回到产生它的 Reply route。

