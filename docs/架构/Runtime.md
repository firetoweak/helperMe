# Runtime

> **Session 是持续的 Event 流；State 是 Event 的确定性归约；模型每次通过一个 Step 作出决策；外部副作用由 Command 执行，并以 Outcome Event 回到下一轮。**

```text
Event → State → Step → Command → Outcome → Event
```

## 职责与边界

Runtime Core 位于 `helperme/runtime`，只负责 Event 持久化、State 归约、Step 原子提交和 Command 派发约束。

Runtime 不理解 Conversation、Context、Criteria、MCP、Skill，也不解释工具结果中的领域含义。目标是否满足、事实意味着什么、下一步做什么，由模型、显式 Judge 或用户决定。

会话入口负责选择 Session identity，Assistant 负责模型决策与异步调度，Dispatcher 负责执行已经提交的 Command。它们都不能成为第二个状态所有者。

## 核心模型

### Session

Session 是 Journal 中的一条持久执行生命线，不是一次函数调用、一次用户问答或一个进程内循环。普通对话会持续追加 Event，并通过多个 Step 向前推进。

Session identity 由 Channel 或 SubAgent Host 选择。Runtime 只持久化并推进给定 identity，不解释它属于聊天、后台任务还是子 Agent。创建 Session 只是建立生命线，不产生 Event，也不触发决策。

### Event

Event 是已经发生并被接纳的事实，包括外部输入、模型决策、授权结果、工具派发与执行结果。Event 一经提交不可原地修改；恢复不会删除未完成 Attempt。SubAgent 的中断回传见[多活跃会话](多活跃会话.md#subagent-中断回传)。

Journal 中的 Event 是唯一执行事实。系统可以从有序 Event 完整重放任意历史切面的 State，并追溯一次决策所依据的事实及其产生的外部结果。

### State

State 只由有序 Event 确定地归约得到。它描述当前是否可以继续决策，以及有哪些 Command 等待授权或结果。

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

每个活跃 Session 有独立进程与 Journal。Worker 内的 Scheduler 只激活自己的 Session，重复唤醒会合并，同一时刻最多只有一个推进任务；跨 Session 的唤醒与投递经 Host Supervisor 路由。见[多活跃会话](多活跃会话.md)。

这形成三个边界：

1. Channel 提交输入并唤醒 Session，不等待一轮任务全部结束。
2. Dispatcher 独立执行已经提交的 Command，不属于 Step。
3. Outcome 必须先成为 Event，归约后才可能触发下一次 Step。

## 决策一致性

用户消息、要求决策的外部事实、授权拒绝和已经收齐的工具结果，都可以成为下一次决策的起点。它们按 Journal 的接纳顺序消费。

模型调用开始前，Assistant 冻结本次决策所见的事实。调用期间到达的新 Event 只影响后续 Step，不能进入当前上下文。提交模型结果时，Runtime 会确认本次决策起点尚未被消费、冻结依据没有失效、Session 仍可推进，并把决策与 Command 原子提交。条件不再成立时，旧决策不会落入 Journal。

Runtime 不重新解释模型输出，也不会把后来到达的事实偷塞进已经冻结的决策。

### 取消当前自动决策链

`cancel_turn` 只停止调用方当前等待的自动决策链，不终止 Session。若模型 Decision 尚未提交，`DecisionCancelled(trigger_event_id)` 与 `StepCommitted` 原子竞争并消费同一个 trigger Event；若 Step 已经提交且 Command 正在进行，则追加 `StepContinuationCancelled(step_event_id)`。Command 仍会执行完，Outcome 仍会如实进入 Journal，但该 Step 的有效 `decision_on_outcome` 为 false，Outcome 不再自动触发下一次 Step。

取消事实不修改历史 Step 或 Command，不撤销副作用。之后独立到达的 `UserMessageReceived` 仍可触发新 Decision，并看到已经入账的 Outcome。普通运行中输入仍走下述后到消息规则，不隐式触发取消。

## 用户输入与后到消息

运行期间和空闲期间收到的普通文本是同一种用户事实。新消息不会抢占当前 Step，不会取消已经发生的外部操作，也不需要单独的 Interrupt 类型；它改变的是下一次决策从哪里开始。

消息会立即进入 Journal。当前冻结中的 Step 仍按原视图提交，本轮已经签发的工具继续执行并如实记录结果。随后遵守两条规则：

1. **已经开始的旧操作先结束。** 如果新消息看不到的工具已经实际开始执行，新消息要等这些操作产生结果后，才能成为下一次决策起点。尚未执行、仍在等待授权的 Command 不阻塞新消息。
2. **旧结果不再单独续跑。** 如果一组工具结果收齐前已经收到新消息，这组结果不再独自触发一次模型调用。下一次决策由新消息触发，并同时看到这些工具结果。

因此，新消息既不会制造并发决策，也不会让模型在看不到最新指令的情况下按旧结果多走一步。Runtime 只依据事实顺序和决策冻结边界推进，不判断“继续”“停止”等文本含义。

## 副作用与恢复

Command 是 Step 提交时冻结的副作用请求。Dispatcher 只执行已经提交、满足授权且尚未开始的 Command。执行前先记录 Attempt，得到确定结果后再提交 Outcome。

同一 Step 可以签发多个并行 Command。Journal 保留结果真实到达的顺序；需要模型继续判断时，等这一组全部结束后只触发一个后续 Step。无需继续决策的投递或加载操作仍然记录结果，但不会单独唤醒模型。

工具边界负责把已知外部错误转换为确定结果。未预期异常原样暴露。如果 Attempt 已经开始，进程却在 Outcome 提交前退出，该操作通常保持未知，不在恢复时盲目重试。SubAgent 恢复时发现未完成 Attempt，由 Assistant 保留原记录并向父会话报告中断、结果未知，不自动重试。子 Session 的未知异常另由 Assistant 在 Worker 退出前写入回收事实，见[SubAgent](SubAgent.md)；Runtime 不把它改写成 Outcome。

外部边界覆盖建立、调用、结果读取和关闭。已知外部能力失败作为事实交给模型，是否重试或换路由由模型决定；失败结果不保证外部副作用没有发生。MCP 本地连接回收后的已知远端关闭失败保留诊断，不阻断原调用的失败结果；命令管道失败通过 `io_errors` 保留部分输出并说明采集可能不完整。异常组只有全部叶子都是已知外部失败时才转换，未知异常和取消不得降级；操作与清理同时失败时保留两者。支撑决策引擎的模型调用失败另按 Assistant / LLM 边界处理。

恢复已有 Session 时，系统只从 Journal 重建 State 和可丢弃的 Host 投影；只有重建后的 State 本身允许继续决策，才会重新唤醒。具体领域如果需要查询、补偿或人工处置未知操作，应建立自己的窄协议，而不是依赖通用恢复状态机。

## 状态

| 状态 | 含义 |
|---|---|
| `RUNNABLE` | 存在尚未消费且当前可执行的决策事实 |
| `WAITING` | 等待用户输入、授权或 Command Outcome |

Session 是持续追加的 Event 流，没有绝对终态。`RUNNING` 是 Scheduler 正在执行模型调用或 Dispatcher 正在执行 Command 的瞬时状态，不是 Journal 归约出的 Runtime 状态。任务完成是模型、Judge 或 Host 的业务判断；Worker 退出、Session 归档和数据删除分别属于 Host、产品和存储生命周期。

## 持久化与重放

Journal 保证：

- 空 Session 也能持久保存 identity；
- 外部投递按来源和投递 identity 幂等接纳；
- Event 在单个 Session 内严格有序；
- 一个决策起点只被 `StepCommitted` 或 `DecisionCancelled` 消费一次；
- 一个 Step 的自动续步权最多被 `StepContinuationCancelled` 关闭一次；
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
| Session 进程与路由 | `helperme/assistant/host/supervisor.py`、`helperme/assistant/host/worker.py` |
| Channel | `helperme/channels/` |

## 决策附带元数据

`RecordedDecision.decision_metadata` 随 `StepCommitted` 原子持久化；Runtime 仅冻结和序列化该不透明 JSON，不据此调度或解释应用语义。Assistant 用它保存 [LoopGuard](LoopGuard.md) 的实际提醒文本、策略版本、证据与覆盖位置。未提交的 Step 不产生提醒覆盖事实。事件格式为 v5，不读取旧格式。
