# Runtime

> **Session 是持续的 Event 流；State 是 Event 的确定性归约；模型每次通过一个 Step 作出决策；外部副作用由 Command 执行，并以 Outcome Event 回到下一轮。**

```text
Event → State → Step → Command → Outcome → Event
```

## 职责与边界

Runtime Core 位于 `helperme/runtime`，只负责 Event 持久化、State 归约、Step 原子提交、Command 派发约束和显式终态屏障。

Runtime 不理解 Conversation、Context、Criteria、MCP、Skill，也不解释工具结果中的领域含义。目标是否满足、事实意味着什么、下一步做什么，由模型、显式 Judge 或用户决定。

会话入口负责选择 Session identity，Assistant 负责模型决策与异步调度，Dispatcher 负责执行已经提交的 Command。它们都不能成为第二个状态所有者。

## 核心模型

### Session

Session 是 Journal 中的一条持久执行生命线，不是一次函数调用、一次用户问答或一个进程内循环。普通对话会持续追加 Event，并通过多个 Step 向前推进。

Session identity 由 Channel 或 SubAgent Host 选择。Runtime 只持久化并推进给定 identity，不解释它属于聊天、后台任务还是子 Agent。创建 Session 只是建立生命线，不产生 Event，也不触发决策。

### Event

Event 是已经发生并被接纳的事实，包括外部输入、模型决策、授权结果、工具派发与执行结果。Event 一经提交不可原地修改。

Journal 中的 Event 是唯一执行事实。系统可以从有序 Event 完整重放任意历史切面的 State，并追溯一次决策所依据的事实及其产生的外部结果。

### State

State 只由有序 Event 确定地归约得到。它描述当前是否可以继续决策、有哪些 Command 等待授权或结果，以及 Session 是否进入终态。

State 可以缓存，但缓存不是事实源。缓存丢失、过期或升级后，都应当能够从 Journal 重建。

### Step

Step 是一次原子的模型决策：

```text
消费一个决策事实
→ 冻结本次决策视图
→ 调用一次模型
→ 提交模型决策
→ 在同一事务中签发零到多个 Command
```

Step 不包含工具执行。一个 Session 可以有任意多个 Step，一次用户交互也可能跨越多个 Step。Runtime 每次推进最多提交一个 Step，不负责用内部循环一直运行到空闲。

## 事件驱动推进

```text
外部 Event 提交
        ↓
Session 可以继续决策
        ↓ wake
Scheduler 激活对应 Session
        ↓ 每次至多一个 Step
Step 提交 ───────────────┐
        ↓                │
Dispatcher 执行 Command  │
        ↓                │
Outcome Event ───────────┘ wake
```

Scheduler 只是 Event 与异步推进任务之间的激活器，不决定不同 Session 的执行顺序。不同 Session 默认并行；同一 Session 的重复唤醒会合并，并且同一时刻最多只有一个推进任务。

这形成三个边界：

1. Channel 提交输入并唤醒 Session，不等待一轮任务全部结束。
2. Dispatcher 独立执行已经提交的 Command，不属于 Step。
3. Outcome 必须先成为 Event，归约后才可能触发下一次 Step。

## 决策一致性

用户消息、要求决策的外部事实、授权拒绝和已经收齐的工具结果，都可以成为下一次决策的起点。它们按 Journal 的接纳顺序消费。

模型调用开始前，Assistant 冻结本次决策所见的事实。调用期间到达的新 Event 只影响后续 Step，不能进入当前上下文。提交模型结果时，Runtime 会确认本次决策起点尚未被消费、冻结依据没有失效、Session 仍可推进，并把决策与 Command 原子提交。条件不再成立时，旧决策不会落入 Journal。

Runtime 不重新解释模型输出，也不会把后来到达的事实偷塞进已经冻结的决策。

## 用户输入与后到消息

运行期间和空闲期间收到的普通文本是同一种用户事实。新消息不会抢占当前 Step，不会取消已经发生的外部操作，也不需要单独的 Interrupt 类型；它改变的是下一次决策从哪里开始。

消息会立即进入 Journal。当前冻结中的 Step 仍按原视图提交，本轮已经签发的工具继续执行并如实记录结果。随后遵守两条规则：

1. **已经开始的旧操作先结束。** 如果新消息看不到的工具已经实际开始执行，新消息要等这些操作产生结果后，才能成为下一次决策起点。尚未执行、仍在等待授权的 Command 不阻塞新消息。
2. **旧结果不再单独续跑。** 如果一组工具结果收齐前已经收到新消息，这组结果不再独自触发一次模型调用。下一次决策由新消息触发，并同时看到这些工具结果。

因此，新消息既不会制造并发决策，也不会让模型在看不到最新指令的情况下按旧结果多走一步。Runtime 只依据事实顺序和决策冻结边界推进，不判断“继续”“停止”等文本含义。

通用 Cancel、外部进程终止和补偿协议目前没有纳入 Runtime。

## 副作用与恢复

Command 是 Step 提交时冻结的副作用请求。Dispatcher 只执行已经提交、满足授权且尚未开始的 Command。执行前先记录 Attempt，得到确定结果后再提交 Outcome。

同一 Step 可以签发多个并行 Command。Journal 保留结果真实到达的顺序；需要模型继续判断时，等这一组全部结束后只触发一个后续 Step。无需继续决策的投递或加载操作仍然记录结果，但不会单独唤醒模型。

工具边界负责把已知外部错误转换为确定结果。未预期异常原样暴露。如果 Attempt 已经开始，进程却在 Outcome 提交前退出，该操作保持未知：Runtime 不把它伪装成未执行，也不会在恢复时盲目重试。

恢复已有 Session 时，系统只从 Journal 重建 State 和可丢弃的 Host 投影；只有重建后的 State 本身允许继续决策，才会重新唤醒。具体领域如果需要查询、补偿或人工处置未知操作，应建立自己的窄协议，而不是依赖通用恢复状态机。

## 状态与终态

| 状态 | 含义 |
|---|---|
| `RUNNABLE` | 存在尚未消费且当前可执行的决策事实 |
| `WAITING` | 等待用户输入、授权或 Command Outcome |
| `COMPLETED` | 有界 Session 的 Host 已显式确认完成 |
| `TERMINATED` | 有界 Session 的 Host 已显式确认终止 |

CLI 和 Telegram Session 是持续对话，普通回答结束后回到等待输入，不自动进入终态。SubAgent 交回结论后同样停在等待状态，“最多回收一次”由投递幂等保证。

终态只为未来明确有界的任务保留，必须由边界外的 Host 在完成 Judge 或 Policy 后显式请求。终态屏障只验证机械条件，不判断目标是否真的完成，也不负责删除 Journal 数据。

## 持久化与重放

Journal 保证：

- 空 Session 也能持久保存 identity；
- 外部投递按来源和投递 identity 幂等接纳；
- Event 在单个 Session 内严格有序；
- 一个决策起点只被消费一次；
- Step 和 Attempt 的领取与提交具有原子约束；
- 一个 Command 最多产生一个 Attempt，一个 Attempt 最多产生一个终态 Outcome；
- 持久格式和重放缓存精确版本化。

Runtime 不承诺外部副作用只发生一次。它承诺所有已知事实可重放、可审计，并在结果未知时不擅自重试。

## 投影边界

模型上下文、执行 Trace、重放清单和 Checkpoint 都是 Journal 的投影。它们可以跨多个 Step 聚合，也可以按产品需要优化、丢弃和重建，但不能反向成为 Runtime 状态所有者。

Checkpoint 只用于加速重放，必须携带足够的版本信息；投影规则或事实结构变化后，旧 Checkpoint 应明确失效。

## 实现位置

| 设计对象 | 当前实现 |
|---|---|
| Journal | `helperme/runtime/journal/` |
| State 归约 | `helperme/runtime/state.py` |
| Step 提交 | `helperme/runtime/step.py`、`helperme/runtime/runtime.py` |
| Command 执行 | `helperme/runtime/dispatcher.py` |
| Session 激活 | `helperme/assistant/runner.py` |
| Session 应用服务 | `helperme/assistant/sessions.py` |
| Channel | `helperme/channels/` |
