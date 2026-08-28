# Runtime 状态推进模型

> **Session 是一条持续存在的 Event 流；State 是 Event 的确定性归约；一次决策闭环是一个 Step；每次激活最多执行一个 Step，不同 Session 独立并行；Dispatcher 独立执行 Command，并以 Outcome Event 再次激活 Session。**

## 1. 四个核心对象

### Session

Session 是 Journal 中的一条持久执行生命线，不是一次函数调用、一次用户问答，也不是某个 `while` 循环。普通 Channel 会话会持续追加 Event，并靠一个个 Step 向前推进。

Session identity 由 Channel、Automation 或 SubAgent Host 选择。Runtime 只持久化并推进给定 identity，不解释它对应聊天、后台任务还是子 Agent。

### Event

Event 是已经发生并被接纳的事实。典型 Event 包括：

- `UserMessageReceived`
- `DomainFactCommitted`
- `StepCommitted`
- `CommandAuthorized` / `CommandRejected`
- `DispatchAttemptStarted`
- `CommandOutcomeReceived`
- `RuntimeCompleted` / `RuntimeTerminated`

外部输入、Step 提交和 Command Outcome 都会改变 Session 的可推进性。Event 一经提交不可原地修改。

### State

Canonical State 只由有序 Event 归约得到。它回答：

- 是否存在尚未消费的决策 Event；
- 当前是否 `RUNNABLE`；
- 哪些 Command 等待授权、派发或 Outcome；
- 是否已经进入显式终态。

State、Checkpoint、Context 和 Trace 都不是第二事实源。Checkpoint 只是带版本和指纹的重放缓存。

### Step

Step 是一次原子的决策闭环：

```text
一个决策 Event
→ 冻结 Decision State
→ 调用一次模型
→ 提交一个 ModelDecision
→ 同一事务签发零到多个 Command
```

Step 不是工具执行循环。一个 Session 包含任意多个 Step；一次用户交互也可能跨多个 Step。

## 2. 事件驱动推进

```text
外部 Event commit
        │
        ▼
Session 变为 RUNNABLE
        │ wake(session_id)
        ▼
SessionScheduler 激活对应 Session
        │ 每个 Session 最多执行一个 Step
        ▼
StepCommitted ───────────────┐
        │                    │
        ▼                    │
Dispatcher 启动 Commands     │
        │                    │
        ▼                    │
CommandOutcomeReceived ──────┘ wake(session_id)
```

SessionScheduler 是 Event 到异步 Session 推进任务之间的激活器，不拥有跨 Session 的执行顺序。每次激活只调用一次 `AgentRuntime.advance(session_id)`；提交后的 State 仍为 `RUNNABLE` 时再次激活。不同 Session 各自拥有推进任务并默认并行，同一 Session 的重复 wake 合并，且同时最多存在一个推进任务。

这带来三个边界：

1. Channel 只负责提交输入并唤醒 Session，不等待“本轮跑完”。
2. Dispatcher 不属于 Step；它独立执行已经提交的 Command。
3. Outcome 是新的 Event；只有它被归约后，才可能产生后续 Step。

同一 Session 的 Step 通过激活 single-flight、claim 和本地串行锁避免并发提交；不同 Session 之间不建立 happens-before 关系。模型 Provider、Sandbox 或外部服务可以在各自边界实施资源限制，但不能由 SessionScheduler 预先串行化独立 Session。

## 3. 决策 Event 与冻结视图

以下事实可以请求决策：

- 每一条 `UserMessageReceived`（是否**当前可执行**见第 6 节）；
- `requests_decision=true` 的 `DomainFactCommitted`；
- 被拒绝且需要模型继续处理的 Command；
- `decision_on_outcome=true` 且并行组已经闭合的 Command Outcome（签发之后若已有后到的 `UserMessageReceived`，则不再请求，见第 6 节）。

决策 Event 按 Journal 接纳顺序消费。模型调用开始后到达的新 Event 只参与后续 Step，不改变当前冻结的 Decision State。

Step 提交时必须重新验证：

- trigger 尚未被其他 Step 消费；
- claim 仍有效；
- decision cursor、basis state version 和 observed journal position 一致；
- Session 未进入终态；
- Command 与 ModelDecision 完全对应。

验证失败就不提交旧 Step。Runtime 不重解释模型输出，也不把新 Event 偷塞进旧上下文。

## 4. Command 与 Dispatcher

Command 是 Step 提交时冻结的副作用请求。当前 Runtime 只有 `InvokeTool`，并冻结：

- 工具名与参数；
- 是否需要授权；
- Outcome 是否请求后续决策。

Dispatcher 只派发已经提交、已满足授权且尚无 Attempt 的 Command。派发前原子写入 `DispatchAttemptStarted`，然后调用 Tool Binding；得到确定结果后写入 `CommandOutcomeReceived`。

同一 Step 中 `decision_on_outcome=true` 的 Commands 构成无序并行组。Journal 保留真实 Outcome 到达顺序，但只有整组闭合后才请求一个后续 Step。`decision_on_outcome=false` 的投递、加载等 Command 仍完整记录 Outcome，但不单独触发模型。

Runtime 不解析工具返回值中的 `ok`、`code` 或其他领域字段。Tool Adapter 在外部输入边界把预期错误转换为确定 Outcome；未预期异常保留原始异常并使当前调度任务失败。

## 5. 未知 Attempt

如果 `DispatchAttemptStarted` 已提交，但进程在写入 Outcome 前失败，Attempt 保持 `unknown`。

当前设计不提供通用 Recovery、Reconcile、Retry 或 Cancel，也不自动重新派发未知 Command。运行中的用户输入不是 Cancel，也不另建 Interrupt Event，见第 6 节。`/resume` 只做三件事：

1. 选择已经存在的 Session；
2. 从 Journal 重建 State 和 Host 投影；
3. 如果 State 本身合法 `RUNNABLE`，重新唤醒它。

恢复不能把未知副作用伪装成未执行，也不能凭模型判断创建第二次调用。未来若某个具体领域需要补偿或人工处置，应在该领域以显式事实和窄协议设计，不在 Runtime 预建通用恢复状态机。

## 6. 用户输入与后到消息

所有普通文本都按 Channel delivery 顺序写成 `UserMessageReceived`。运行中说「不对」「不要继续」「继续」，和 `WAITING(user_message)` 时再说一句，是同一种外部事实：人改的是**下一次决策从哪来**，不是另开中断协议。

Runtime 不读这句话的含义，也不区分空闲 / 运行中输入的类型。没有 `InterruptReceived`，没有抢占当前 Step 的特殊消息，也不提供通用 Cancel。Channel 不维护对应用户消息的 worker 或 Run 生命周期；`Ctrl+C` / `Ctrl+D` 只退出进程。后到消息与 `TerminationRequested` / `TERMINATED` 无关：Channel 对话生命线在消息之后仍继续。

Channel 接纳即时：文本一旦通过 delivery 幂等写入 Journal 就可以展示并 wake。当前冻结中的 Step 仍基于冻结视图提交完毕。该 Step **已经签发**的 Command 继续派发；已经 `DispatchAttemptStarted` 的 Attempt 跑到真实 Outcome。这只覆盖这一轮 Step 里的那一批 Command，不是批准旧任务再开下一轮。Runtime 不杀工具进程、不写伪装的取消结果、不收拾残局。

后到的 `UserMessageReceived` 只改写下一次决策的起点。归约多两条机械规则。

### 规则 1：这条 UserMessage 现在能不能当 `next_trigger`

消息立刻入账，但它是不是**当前可执行的决策 Event**，取决于有没有它看不见的、已经动手、尚未结束的副作用：

> 若存在签发 Step 的 `observed_journal_position` 早于这条消息、已经 `DispatchAttemptStarted`、尚未终态的 Command，这条 UserMessage 还不是可执行决策。等这些 Attempt 都有 Outcome 之后，它才成为 `next_trigger`。

这包括：消息入账前就已派发的工具；以及模型还在跑时入账、该 Step 随后才提交并派发的同一轮 Command。后一种 Command 的签发 Event 序号可能大于消息，但冻结视图里没有这句话，仍算这句话要等的「这一轮」。

| 前面是什么 | 等不等 |
|---|---|
| 模型还在跑、Step 尚未提交 | 先等当前 Step 提交（同一 Session 本就 single-flight） |
| 上述 Step 提交后，同一轮已派发工具还在跑 | 等这批全部终态 |
| 更早签发、已派发、工具还在跑 | 等这批全部终态 |
| 已签发、还卡在授权、没有 Attempt | 不等；否则与 yes/no 死锁 |
| 空闲，`WAITING(user_message)` | 不等，立刻可执行 |

等的是已经动手的进程，不是整份 Session 忙闲。未派发的 Command（含待授权）不阻塞新消息；它们仍按原规则等待授权或 Dispatcher，不因此被写成取消。

### 规则 2：旧那批 Outcome 还要不要自己再要一次 Step

今天并行组闭合后，合格 Outcome 会请求下一次 Step。后到的 UserMessage 否决这次自动续跑：

> 若该组签发 Step 的冻结视图（`observed_journal_position`）之后已经出现 `UserMessageReceived`，这组闭合时不再单独请求 Step。下一次决策的 trigger 是那条 UserMessage。

「之后」相对冻结切面，不是相对 `StepCommitted` 落盘序号。模型还在想时入账的话，Journal 上会写在该 Step 提交之前，但它已经晚于冻结视图，仍是否决续跑。

Outcome 照常写入。改的只是「要不要为这组 Outcome **再开一轮**模型」。同一轮里多个 Command 的派发与跑完不是「再批准」。没有后到 UserMessage 时，行为与现在相同。有后到 UserMessage 时，那条消息吃掉无人值守续跑——包括工具还在跑时说的，也包括工具刚结束、模型还在想或还没开始想时说的。

冻结视图因此带着真实 Outcome 与这句用户话，一次决策看完。多条后到消息仍按 Journal 顺序消费，谁也不插队；每一条吃掉的是**它之前那批**的自动续跑。签发 Step 冻结时已经看见的排队消息是 FIFO，不按后到插队。

「掐掉」的不是工作本身。当前 Step 会提交；这一轮已签发的工具仍跑完。模型若在**下一拍**（后到消息当 trigger 的那次）把「继续」理解成接着干，就在那一拍接着干。Runtime 不猜「继续」和「停下」的差别，也不再为旧组自动开一轮让模型在看不见新指令的上下文里续跑。

审计不需要新 Event 类型。重放时只要看到某条 UserMessage 晚于一次签发 Step 的 `observed_journal_position`——无论它落在 `StepCommitted` 与该批 Outcome 之间、落在组闭合之后、还是落在该 StepCommitted 之前——就是一次后到输入改写了决策起点。

## 7. Runtime Status

| Status | 含义 |
|---|---|
| `RUNNABLE` | 存在尚未消费且当前可执行的决策 Event |
| `WAITING` | 暂无 Step；等待用户输入、授权或 Command Outcome |
| `COMPLETED` | 有界 Session 的 Host 已显式完成 finalization |
| `TERMINATED` | 有界 Session 的 Host 已显式终止 finalization |

普通 CLI / Telegram Session 不自动进入终态。模型的 `LifecycleIntent.complete/terminate` 只是声明；Channel 不调用 `finalize()`，完成一次输出后仍回到 `WAITING(user_message)`。

终态仅保留给明确有界的后台任务或 SubAgent Session，由它们的 Host 在边界外完成 Judge/Policy 后显式调用。Finalization Barrier 只验证机械条件，不判断目标是否真的完成。终态表示逻辑关闭，不负责删除 Journal 数据。

## 8. 持久化与幂等

Journal 必须保证：

- Session identity 可在零 Event 时持久存在；
- `(delivery_source, delivery_id)` 幂等；
- Event sequence 在单 Session 内严格递增；
- Step trigger 只消费一次；
- Step claim、Attempt claim 和提交具有原子约束；
- Command 只创建一个 Attempt；
- 每个 Attempt 只有一个终态 Outcome；
- Codec、Checkpoint 和 SQLite schema 精确版本化。

Runtime 不承诺外部副作用 exactly-once。它承诺所有已知事实可审计，不在缺少 Outcome 时盲目重试。

## 9. 投影边界

- Context：下一 Step 的模型输入；
- Trace：执行与因果观察；
- Replay Manifest：重建一次模型请求所需的 Assistant 产物。

这些投影可以跨多个 Step，也可以按产品需要重新聚合，但都不能反向成为 Runtime 状态所有者。

## 10. 实现映射

| 设计对象 | 当前实现 |
|---|---|
| Journal | `helperme/runtime/journal/` |
| State reducer | `helperme/runtime/state.py` |
| Step claim/commit | `helperme/runtime/step.py`、`runtime.py` |
| Command dispatcher | `helperme/runtime/dispatcher.py` |
| Session activator | `helperme/assistant/runner.py` |
| Session application service | `helperme/assistant/sessions.py` |
| Channel | `helperme/channels/` |

验收重点不是“一个调用是否跑到 idle”，而是：每个 Event 是否可靠唤醒、每次激活是否只提交一个 Step、不同 Session 是否能独立并行、Outcome 是否再次驱动 Session、进程重启后是否只从 Journal 重建事实、未知 Attempt 是否保持未知，以及后到 `UserMessageReceived` 是否只改写下一拍决策起点（规则 1 等待签发冻结早于该消息的已派发 Attempt；规则 2 按冻结切面抑制旧组自动续跑，不抑制同一轮已签发 Command 跑完）。
