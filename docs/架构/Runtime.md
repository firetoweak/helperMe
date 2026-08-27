# Runtime

Runtime Core 是 `helperme/runtime`。它只负责 Event 持久化、State 归约、Step 原子提交、Command 派发约束和显式终态屏障。

Session 是持续 Event 流，不是一次用户消息的执行循环。Channel 或 Automation 选择 identity，`create_session(identity)` 幂等持久化生命线；创建本身不是 Event，也不触发 Step。

一次推进的原子单元是 Step：消费一个决策 Event，冻结 Decision State，调用一次模型，提交一个 Decision，并原子签发 Commands。`AgentRuntime.advance(session_id)` 最多提交一个 Step，不负责把 Session 循环跑到 idle。

`SessionScheduler` 监听事实提交后的 wake：外部输入激活对应 Session；Step 提交后 Dispatcher 独立启动 Commands；Outcome Event 再次激活 Session。Scheduler 不属于 Runtime Core；不同 Session 的推进任务默认并行，同一 Session 保持 single-flight。

Canonical State 由 Journal 重放得到：

| Status | 含义 |
|---|---|
| `RUNNABLE` | 有一个合法的待消费决策 Event |
| `WAITING` | 等待输入、授权或 Command Outcome |
| `COMPLETED` | 有界 Host 已显式完成 finalization |
| `TERMINATED` | 有界 Host 已显式终止 finalization |

CLI / Telegram Session 是持续对话，永不因普通回答自动终态化。`COMPLETED / TERMINATED` 只供未来有界后台任务或 SubAgent Host 显式使用。

所有 Channel 文本统一为有序 `UserMessageReceived`。运行中到达的新消息不会抢占或取消冻结中的 Step，也不另建 Interrupt Event；它改写的是下一次决策从哪来。已派发 Attempt 先跑完并记账，旧 `decision_on_outcome` 组不再单独要 Step。见 [Runtime 状态推进模型](Runtime状态推进模型.md) 第 6 节。

Command 当前只有 `InvokeTool`。Dispatcher 先持久化 Attempt，再执行 Binding，最后写入确定 Outcome。未预期异常原样穿透；已经开始但没有 Outcome 的 Attempt 保持 `unknown`。Runtime 不提供通用 Recovery、Reconcile、Retry 或 Cancel，也不会在 `/resume` 时盲目重试。

`/resume` 只选择已有 Session、重建投影，并在 State 本身为 `RUNNABLE` 时唤醒。Journal、Step claim、Attempt claim、投递幂等和 Finalization Barrier 仍提供机械一致性。

Runtime 不理解 Conversation、Context、Criteria、MCP、Skill 或工具返回值中的领域字段。完整决策见 [Runtime 状态推进模型](Runtime状态推进模型.md)。
