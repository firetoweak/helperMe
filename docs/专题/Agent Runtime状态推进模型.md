# Agent Runtime 状态推进模型

> 状态：架构决策，作为新 Runtime 的目标模型。  
> 决策日期：2026-08-21  
> 来源：[Execution Journal 与可重放执行历史](Execution%20Journal与可重放执行历史.md) 的继续推演。  
> 适用方式：从新模型本身推导设计；现有 HelperMe Runtime 只作为需求、测试与迁移来源，不构成本模型的兼容约束。

## 1. 决策

新 Agent Runtime 采用事件驱动的状态推进模型：

> **Event 是唯一持久执行事实，State 是事件归约结果，Step 是决策推进单元，Command 是外部副作用闭环，Turn 是人类交互投影。**

```text
                      Turn / Context / Trace
                             Projections
                                  ▲
                                  │
                          Execution Journal
                                  │
                               reduce
                                  ▼
                         Canonical State
                                  │
                              evaluate
                                  ▼
                         Runtime Status
                ┌─────────┬───────┴───────┬──────────┐
                │         │               │          │
             RUNNABLE   WAITING       COMPLETED  TERMINATED
                │         ▲
                ▼         │
              Step        │ External Event
                │         │
                ▼         │
        Decision + Commands
                │
                ▼
            Dispatcher
                │
                └───────────────→ Events
```

这项决策替代以下旧前提：

- `TurnRuntime` 是执行核心；
- Conversation 是运行主事实；
- 一次 AgentStep 必须等待整个工具批次结束；
- Runtime State 先变化，Journal 事后记录；
- Interrupt 是 Turn 内的特殊控制流。

新 Core 不以保留旧抽象或兼容旧运行协议为设计目标。迁移方案在核心语义稳定后单独决定。

## 2. 为什么改变

Execution Journal 最初只用于可观测历史与无副作用重放，但进一步推演后发现，同一组稳定语义已经同时出现在真实需求与明确路线中：

- 用户输入、工具结果和中断都是外部事实；
- 并行工具需要按结果到达顺序重新决策；
- 长时间任务不能让一次调用栈持续等待；
- 后台任务需要跨进程等待、恢复和继续；
- SubAgent 需要独立执行历史与因果关联；
- Context、Turn、Trace 和 Memory 都需要从同一事实历史生成不同视图。

如果 Journal 只做事后记录，这些能力仍要各自维护运行状态、等待位置和恢复语义。让 Journal 成为运行时事实骨架后，它们可以共享同一个状态推进模型，而各自保留领域策略。

这是已经出现的共同语义，不是为未知框架使用者预建通用事件平台。

## 3. 核心概念与权威边界

### 3.1 Stream

Stream 是能够独立排序、推进、等待和恢复的一条执行生命线。

第一版只要求：

- 一个 Stream 内拥有权威事件顺序；
- 同一时刻最多有一个 Step 正在为该 Stream 做模型决策；
- 不同 Stream 可以并行推进；
- 父子执行未来通过显式因果引用关联，不共享隐式可变状态。

Stream 与 Session、Goal、后台任务或 SubAgent 的具体映射暂不在 Core 中冻结，等垂直切片验证后再确定。

### 3.2 Event

Event 表示：

> 系统已经确认发生过什么。

Event 不可变，只能追加。它记录事实，不解释事实，也不决定下一步。

```text
UserMessageReceived
UserInterruptReceived
StepCommitted
CommandDispatchStarted
ExternalOperationAccepted
ToolResultReceived
RuntimeCompletionDeclared
```

以上只是语义示例，不是第一版必须一次性建立的通用事件清单。

Journal 是系统内部已确认执行事实的唯一语义权威与顺序索引。大型正文位于 Artifact Store；Journal 中的稳定引用、校验值和对应的不可变 Artifact 共同构成可重放事实。Artifact 不独立拥有运行语义。

事件信封至少需要表达：

```text
event_id
stream_id
sequence
event_type
causation_id?
correlation_id?
occurred_at
schema_version
payload
artifact_refs
```

具体字段名由实现决定，但稳定身份、流内顺序、直接因果、版本和有界载荷不能缺失。`sequence` 表示 Journal 原子接纳的权威顺序；并发回调不存在可可靠证明的“真实先后”，`occurred_at` 只用于观测，不能覆盖 `sequence` 的裁决地位。

外部输入边界必须提供稳定 delivery identity 或等价的持久去重语义。同一 delivery 重投不能产生第二个决策 Event；来源不提供身份时，Adapter 必须在确认接收前先建立自己的持久 receipt。

### 3.3 State

State 表示：

> 对当前已提交 Event 的确定性归约结果。

Reducer 必须是纯逻辑：相同事件及相同 Artifact 内容产生相同 State，不调用模型、工具或其他外部系统。

```text
State = reduce(previous_state, event)
```

State 可以包含：

- 已确认的目标、约束、计划和证据；
- 按顺序等待决策消费的 Event；
- Command、执行尝试和外部操作状态；
- 当前 Step claim 或恢复位置；
- Runtime 生命周期状态所需事实。

Checkpoint 只是某个 Journal 位置上的 State 快照，用于加速恢复。删除 Checkpoint 后必须仍能通过 Journal 重建 State，因此 Checkpoint 不是第二份权威事实。

`Canonical State` 是逻辑上的一致状态集合，不是把 Goal、Plan、Evidence、Scheduler 和所有 Plugin 字段塞进一个 Core 大对象。Core 只组合调度、决策消费、Command 和生命周期所需的窄 Projection；具体能力拥有自己的 Event 载荷、Reducer 和领域 State。Canonical 表示它们基于同一事实版本彼此一致，不表示它们共享一个领域模型。

### 3.4 Step

Step 表示：

> Agent 在一个冻结的决策状态上完成一次 LLM 决策，并原子提交该决策及其 Commands。

Step 闭合的是一次**决策因果链**，不是它产生的所有外部副作用。

### 3.5 Command、Attempt 与 Outcome

Command 表示 Runtime 已经决定请求外部世界执行的动作。`CommandIssued` 是事实，但不代表外部动作已经发生。

Attempt 表示 Dispatcher 对 Command 的一次实际执行尝试。Outcome 表示已确认的最终结果。

Tool 是 Command 的一种执行适配器，不属于 Step 事务，也不拥有 Agent 生命周期。

### 3.6 Turn

Turn 表示人类理解的一次交互过程，是对相关 Event、Step 和 Outcome 的聚合视图。

Turn 不负责：

- 调度 Step；
- 持有工具批次；
- 定义中断安全点；
- 决定 Runtime 是否完成。

Turn 可以被删除后重建，不是运行权威。

## 4. 运行主链

```text
External Event
      │
      ▼
Append Journal
      │
      ▼
Reduce Canonical State
      │
      ▼
Evaluate Runtime Status
      │
      ├─ WAITING ─────→ 等待新的 External Event
      │
      ├─ COMPLETED
      │
      ├─ TERMINATED
      │
      └─ RUNNABLE
             │
             ▼
        Claim next decision input
             │
             ▼
        Freeze Decision State
             │
             ▼
          LLM Decision
             │
             ▼
   Atomically commit Decision + Commands
             │
             ▼
     Dispatcher executes Commands
             │
             ▼
          Append Events
```

Evaluator 只能根据 State 做确定性调度判断，不得在隐藏路径中调用 LLM。如果“目标是否已经满足”需要语义判断，它必须由普通 Step 或显式 Judge Step 作出并留下决策事实。

## 5. Event 到达与 Step 消费

Event 写入 Journal 和 Event 被 Agent 决策消费是两件事。

外部 Event 到达后立即成为持久事实。Reducer 可以把它归约为：

- 只需确定性更新 State 的普通事实；
- 等待 Agent 决策的有序输入。

Runtime 每次选择最早尚未处理的决策输入。这里的顺序严格指 Journal 为外部输入分配的接纳顺序。当前约束是：

> 一个 Step 只以一个决策 Event 为直接触发原因；需要决策的 Event 按流内到达顺序分别处理。

`ToolStarted`、心跳或进度等 Event 可以只更新 State，不创建 Step。`ToolSucceeded`、`ToolFailed`、`UserInterrupt` 等通常形成决策输入，但最终规则由具体领域契约决定，不以事件名称硬编码在通用 Journal 中。

Step 必须留下足以重建其决策边界的信息，语义上至少包括：

```text
step_id
trigger_event_id
decision_cursor
basis_state_version
observed_journal_position
model_decision_reference
issued_command_ids
```

这些是语义要求，不代表最终必须使用同名字段。

当多个 Event 已经到达时，Canonical State 可以知道它们正在排队，但处理前一个 Event 的 Decision State 不得提前暴露后续决策输入。后续 Event 在当前 Step 提交后分别触发新的 Step。

因此 Decision State **不是简单的 `Journal[0..sequence]` 前缀**。Runtime 至少维护两个正交位置：

```text
journal_position   已经持久接纳到哪里
decision_cursor    Agent 已经按顺序决策消费到哪里
```

决策投影按以下逻辑顺序构造：

```text
上一版已提交 Decision State
        ↓
按 Journal sequence 读取下一个尚未消费的输入
        ↓
普通事实直接归约；遇到决策 Event 时暂停
        ↓
将该 Event 应用于冻结视图并执行 Step
        ↓
把引用该 Event 的 Step Commit 应用于决策视图
        ↓
推进 decision_cursor，再处理后续输入
```

Step Commit 即使在物理 Journal 中排在后来到达的 Event 之后，也通过 `trigger_event_id` 被逻辑地归入对应决策 Event 的因果切片。这样处理 A 的 Step 可以看到此前处理 B 的决策，又不会提前看到排在 A 后面的 Interrupt。Projection 必须按这套确定性规则重建，不得把 Journal 尾部直接当成模型可见状态。

当 Event 在模型调用期间到达时：

- Event 可以立即持久化；
- 当前 Step 继续使用已经冻结的 Decision State；
- 新 Event 不改变当前模型输入；
- 当前 Step 提交后，新 Event 按决策消费顺序继续推进。

因此 Journal 的追加尾部与 Step 的决策消费水位是两个明确概念，不能用“读取最新 State”隐式混合。

## 6. Step 提交边界

Step 的逻辑流程是：

```text
选择决策 Event
      ↓
持久 Claim
      ↓
冻结 Decision State
      ↓
构造 Model Context
      ↓
调用 LLM
      ↓
提交 Decision + Commands + 消费位置
```

Step Commit 必须是一个原子 Journal 写入单元。物理上可以是单个复合 Event，也可以是一批原子追加的 Event；核心不变量是不能出现“Decision 已提交但部分 Command 丢失”或相反状态。

Claim 不是进程内布尔值。它至少具有稳定 claim token、持有者和可恢复的失效语义。Step Commit 必须原子验证：

- claim token 仍然有效；
- `trigger_event_id` 尚未被其他 Step 消费；
- 当前 Commit 与冻结的 `basis_state_version` 对应。

同一决策 Event 最多只能成功 Commit 一个 Step。Worker 失联后可以通过显式 lease-expired/reclaim 事实接管；旧 Worker 即使稍后返回模型结果，也会因 token 失效而无法 Commit。具体存储可以使用条件追加、唯一约束或 compare-and-swap，但不能只靠单进程锁表达该不变量。

模型调用本身是 Journal 事务之外的非确定性操作：

- 模型响应成功返回但尚未 Commit 时崩溃，允许恢复后重新调用模型；
- 未 Commit 的模型响应绝不能产生 Command；
- 已 Commit 的模型响应在历史重放时直接读取，不重新调用模型；
- 如果未来需要避免重复模型费用，可以增加 Invocation Attempt 与 Provider 请求身份，但不改变 Step 语义。

Tool execution 不属于 Step：

```text
错误：
Step = Decision + Tool Execution + Results + Commit

正确：
Step = Frozen State + Decision + Durable Command Commit
```

例如：

```text
Step 1 commits Start(A), Start(B), Start(C)

A / B / C 并行执行
B.Succeeded 先到达

Step 2 consumes B.Succeeded
→ Wait(A, C)
或
→ Abandon(A, C) + Cancel(A, C)
```

`Abandon` 表示后续结果不再影响 Agent 决策，是随 Step Commit 的内部决策事实；`Cancel` 表示尽力停止外部执行，是交给 Dispatcher 的外部 Command。两者不能混为一个动作。被 Abandon 的 Command 后续 Outcome 仍写入 Journal，但默认不再触发 LLM Step。

## 7. Command 副作用闭环

Dispatcher 只能执行已 Commit 且已取得派发资格的 Command。外部调用前必须先持久记录执行尝试：

```text
CommandIssued
      ↓
DispatchAttemptStarted       先持久化
      ↓
invoke external system
      ↓
ExternalOperationAccepted?   可选，保存外部操作身份
      ↓
CommandSucceeded / Failed / Cancelled
```

`pending → DispatchAttemptStarted` 必须是原子认领：每个 Attempt 具有稳定身份和 claim token，同一个 Command 不能被两个 Worker 同时创建有效 Attempt。恢复或重试若需要新 Attempt，必须先依据上一 Attempt 的 Outcome 或 Recovery Contract 明确取得资格；要求幂等键的 Command 在多个 Attempt 间继续使用同一个稳定幂等身份。

Authorization 状态与执行状态正交；未取得派发资格的 Command 仍处于 authorization pending/rejected，不进入下面的执行状态机。取得资格后，Command 执行状态由事实归约：

| 状态 | 已知事实 | 恢复动作 |
|---|---|---|
| `pending` | 已签发，但没有执行尝试 | 重新调度 |
| `unknown` | 已开始执行尝试，但没有可靠外部确认或最终结果 | 查询、验证，或进入受约束的 Agent/人工决策 |
| `running` | 外部系统已确认接收，并提供可查询的非终态操作身份 | 查询、等待回调或按契约取消 |
| `terminal` | 已有成功、失败或取消等最终 Outcome | Command 自身不再恢复 |

先记录 `DispatchAttemptStarted`，再调用外部系统，可以保证 `pending` 的重新调度是安全的。代价是进程可能在真正发送前崩溃，使 Command 保守地进入 `unknown`；这是正确的不确定性表达。

Command terminal 不等于 Runtime `TERMINATED`。例如工具失败已经让 Command 结束，但该结果通常会使 Runtime 再次 `RUNNABLE`，由 Agent 选择替代方案。

Command 基于冻结的 Decision State 签发，派发时目标状态可能已经变化。例如 Agent 根据 B 的结果请求取消 A，但 A 在模型决策期间已经完成。Dispatcher 不得假装取消成功，也不应把这种预期竞态升级成 Runtime 崩溃；它应追加 `already_terminal`、`not_applicable` 或具体 Tool Contract 定义的等价 Outcome。原始成功结果仍然保留。

### 7.1 Tool Recovery Contract

Runtime 单侧无法推断外部副作用是否可以重试。每个可执行 Tool Adapter 必须显式声明它能够兑现的恢复契约，而不是依赖工具描述或模型猜测。

最小语义包括：

```text
retry_semantics:
  safe
  idempotency_key_required
  prohibited

reconcile:
  是否能够查询或验证一次 Attempt 的外部结果

cancel:
  是否支持取消，以及取消的终态语义

receipt:
  可用于查询的 external_operation_id 或 verification reference
```

具体工具可以使用更窄的领域类型，不要求所有工具实现统一而庞大的恢复接口。接入边界必须验证声明与实现相符；进入 Runtime 后直接相信契约，失败保留原始语义。

尚未提供专属恢复能力的 Tool 使用保守契约：不可自动重试、不可假设能够 reconcile。缺少恢复能力不能通过通用 fallback 伪装成安全。

模型可以在恢复协议允许的动作之间做逻辑决策，但不拥有安全权限：

- 天气查询声明 `safe`，模型可以选择重试；
- 转账要求稳定幂等键或禁止重试，Runtime 不得因为模型认为“应该没成功”就创建第二次转账；
- 没有查询能力且不可安全重试时，模型只能选择契约允许的等待、终止或请求人工裁决。

Runtime 不承诺外部副作用 exactly-once。它承诺不隐藏不确定性，并使用幂等、查询验证或人工裁决把每个 Attempt 推进到可解释状态。

### 7.2 Authorization 与用户可见输出

“已经随 Step Commit”不自动等于“允许派发”。需要权限或人工审批的动作必须具有持久授权事实：

```text
Step commits CommandIssued
        ↓
Policy evaluates
        ├─ pre-authorized → CommandAuthorized
        ├─ needs approval → WAITING → ApprovalGranted / Rejected
        └─ forbidden      → CommandRejected
        ↓
DispatchEligible = issued + authorized
```

`CommandPhase` 只描述执行生命周期；authorization 是正交的派发门。`PENDING` 表示已签发且尚未终态，不等于 Dispatcher 可以认领。

Policy 是确定性安全边界，不由模型自行放宽。实现也可以在 Step Commit 前完成同步授权检查，但最终 Journal 必须能够证明某个 Command 为什么具有派发资格。持久信任、权限或能力状态的变化继续经过用户审批。

发送消息、提交表单、发布内容等用户或外部系统可见动作同样属于 Command。模型生成期间的流式 token 尚未 Commit：可以作为明确标记的临时 UI 预览，但不能进入 Conversation、驱动外部动作或被表示成已送达消息。需要可靠交付的 Agent 输出必须在 Step Commit 后通过带 delivery identity 的 Command 发送并记录 Outcome。

## 8. Runtime Status

Runtime Status 是 State 的确定性派生结果，不是另一个状态所有者。

| 状态 | 含义 |
|---|---|
| `RUNNABLE` | 存在合法的下一决策输入，可以调度 Step |
| `WAITING` | 当前没有合法 Step，但存在明确的未来唤醒来源 |
| `COMPLETED` | 已有成功完成事实，且没有尚未解决的必要依赖 |
| `TERMINATED` | 已有明确的非成功终止事实，不再允许推进 |

`WAITING` 必须能够说明在等待什么，例如 Command Outcome、Timer、Watcher、用户输入或人工审批。没有等待来源、没有可执行 Step、也没有终态，是非法或需要诊断的 Runtime State，不能静默归类。

权限拒绝、工具失败或取消请求不天然等于 `TERMINATED`。它们首先是事实，是否终止由显式生命周期规则或后续 Step 决定。

Completion/Termination Decision 不能越过已经接纳但尚未消费的决策 Event。Step 只能提交 `CompletionDeclared` 或等价终态意图；Runtime 随后通过确定性 Finalization Barrier 原子验证“没有待消费决策输入、没有必要依赖、生命周期版本未变化”，成功后才追加真正的 `RuntimeCompleted` / `RuntimeTerminated` 事实。

```text
Interrupt accepted
        ↓
older Step commits CompletionDeclared
        ↓
Finalization CAS 因 pending Interrupt 失败
        ↓
Interrupt 优先触发下一 Step
        ↓
重新声明完成 / 恢复运行 / 终止
```

这样终态不是对排队 Event 的覆盖。终态事实成功 Commit 后到达的新用户意图必须进入新的 Stream/interaction epoch，或先追加显式 `RuntimeResumed` 事实；普通 Event 不能隐式复活终态。

Step 的执行 claim、Dispatcher worker lease 等属于并发控制事实，不需要为此把领域状态扩张成另一个含混的 `RUNNING`。它们必须可恢复和可观测，但与 Runtime 是否具有下一步是不同问题。

`COMPLETED` 或 `TERMINATED` 只停止新的 Agent 决策，不拒绝迟到的外部事实。已经派发的 Command 仍可能产生 Outcome，Runtime 必须继续记录并完成资源清理，但这些 Event 不得在没有显式恢复事实时重新开启终态 Stream。

## 9. Projection

同一份 Journal 可以产生多个职责单一的视图：

```text
Execution Journal
├── Canonical State       驱动 Runtime
├── Decision State        冻结给某个 Step
├── Turn Projection       人类交互视图
├── Conversation View     模型协议事实视图
├── Model Context         当前模型输入
├── Trace                 时间线、因果与诊断视图
└── Memory Extraction     未来的知识提取输入
```

### 9.1 Model Context

模型不直接读取完整 Event Stream。每个 Step 的 Model Context 由以下内容投影：

```text
Frozen Decision State
+ Recent relevant Steps
+ Relevant Evidence / Artifact references
+ Current capabilities and constraints
```

模型供应商的 tool-call 消息格式属于 Model Context Adapter，不反向规定 Runtime 必须等待原生工具批次全部完成。Conversation 也不再机械等同于某一家模型 API 的历史消息数组。

每个已 Commit Step 必须保存精确模型请求，或保存足以无歧义重建该请求的 Replay Manifest。其最小语义包括：

```text
Decision State version
Prompt / instruction references
Capability snapshot
Model and inference configuration
Context projector version
Referenced Artifact hashes
Actual model response
```

如果某项输入不能由版本化事实稳定重建，就直接保存当次请求 Artifact。Projector 代码升级后可以生成新的实验投影，但不能冒充原 Step 实际看到的上下文。

### 9.2 Turn Projection

Turn 通过用户输入、因果引用和面向人的结束响应聚合相关事实。它可以跨多个 Step，也可以包含等待和中断，但这些都不改变 Runtime Core。

### 9.3 Trace 与 Checkpoint

Trace 可删除后重建，不参与运行裁决。Checkpoint 可以缩短 State 恢复时间，但必须绑定明确 Journal 位置、State Schema 与恢复契约；它不是普通日志，也不产生新的领域事实。

## 10. 重放与恢复

以下三种操作必须分开：

### 10.1 历史重放

读取已经 Commit 的模型决策、Command Outcome 和外部 Event，重建 State、Turn、Context 或 Trace，不访问外部系统。

Journal、Replay Manifest 和被引用 Artifact 的保留期必须覆盖所承诺的重放与恢复窗口。若 Artifact 被合法删除，对应历史必须明确降级为“只能查看元数据，不能完整重放”，不能静默使用当前文件或新版本内容替代。

### 10.2 Runtime 恢复

从 Checkpoint 与后续 Event 重建 State，根据 Command 状态恢复调度：

```text
pending  → 重新调度
unknown  → 按 Tool Recovery Contract reconcile
running  → 查询、等待或取消
terminal → 无 Command 恢复动作
```

### 10.3 重新执行

重新调用模型或重新签发新的 Command 会产生新的事实与结果，不能冒充原历史重放。需要与原执行建立显式分支或因果引用。

原 Journal 专题中的 L1/L2/L3 不再是三套架构或 L2 停止边界，而是同一个 Runtime 的能力验收层级：

```text
L1 事实可观察
L2 历史可重放
L3 中断后可恢复执行
```

核心语义一次建立，并通过垂直切片逐步完成三个等级。后续等级只补足持久化与恢复能力，不改变 Event、State、Step、Command 和 Projection 的职责。

## 11. 后续能力如何生长

未来能力复用 Core 的运行语义，但不把领域策略写回 Core：

```text
Scheduler
→ 根据时间策略追加 TimerTriggered Event

Watcher
→ 根据监听策略追加 ExternalChangeDetected Event

Background Task
→ 没有前台 Channel 时仍推进同一个 Stream

SubAgent
→ 创建独立子 Stream，以因果 Event 委派和回收结果

Long Memory
→ 从 Journal 派生知识并保留来源引用
```

Journal 不是通用领域事件总线。Core 只拥有顺序、身份、因果、归约、决策提交、Command 执行边界和恢复所必需的稳定机制。

## 12. 必须保持的不变量

1. 一个 Stream 内的 Event 具有稳定身份和权威追加顺序。
2. 外部 delivery 在接入边界具有稳定身份，同一 delivery 最多形成一个 Event。
3. Event 一旦 Commit 不修改；纠错通过追加新事实表达。
4. State 和 Projection 可以删除后从 Journal、有效 Artifact 与受支持的版本化规则重建。
5. 同一 Stream 同一时刻最多存在一个有效 Step claim，同一决策 Event 最多成功 Commit 一个 Step。
6. Step 使用冻结的 Decision State，不观察调用期间到达的新 Event。
7. 需要决策的 Event 按 Journal 接纳顺序消费，每个 Step 明确引用触发 Event 与决策状态版本。
8. Decision 与其全部 Commands 原子 Commit；未 Commit 的 Decision 不产生副作用。
9. Dispatcher 只执行已 Commit 且已授权的 Command，并通过原子 claim 在外部调用前 Commit Attempt。
10. 外部结果只通过 Event 影响 State，Tool 不直接修改 Runtime State。
11. `unknown` 是合法且必须保留的状态，不通过猜测或静默重试消除。
12. 模型只能选择 Authorization 与 Tool Recovery Contract 允许的动作，不能扩张安全权限。
13. Completion Declaration 必须通过无待消费输入的原子 Finalization Barrier 才能成为终态事实。
14. Historical Replay 不调用模型、工具或其他外部系统。
15. Runtime Status 由 State 确定性推导；需要语义判断时显式创建 Step。
16. Turn、Conversation、Context、Trace 和 Checkpoint 都不是第二事实源。

## 13. 明确不做

- 不把每个函数调用、日志或指标都事件化；
- 不预建 Scheduler、Watcher、SubAgent 或 Memory 的领域状态机；
- 不追求跨任意外部系统的 exactly-once；
- 不建设通用分布式 Event Bus、Saga 框架或未知第三方扩展协议；
- 不为了迁移便利保留与新核心冲突的 `TurnRuntime` 或工具批次语义；
- 不一次性定义所有未来 Event 类型和 Schema 兼容策略；
- 不把模型判断当作副作用安全边界；
- 不让 Projection 的缓存或 Checkpoint 反向成为隐藏状态所有者。

## 14. 验证主切片与递进验收

验证始终围绕一条最有区分度的主场景，不为每个机制另造平行原型：

```text
UserMessage
→ Step 1 commits Start(A), Start(B), Start(C)
→ Dispatcher 并行执行
→ B first succeeds
→ Step 2 consumes B result
→ Agent chooses Wait 或 Abandon/Cancel A、C
→ late outcomes 继续写入 Journal
→ 删除 State 后重放得到相同状态与 Turn
→ 在 pending / unknown / running 位置分别模拟重启并验证恢复
```

实现按三次递进验收，后一次只补前一次尚未具备的可靠性：

### 14.1 语义切片

> 已于 2026-08-21 完成独立 MVP，见 [Agent Runtime 语义 MVP 实现总结](Agent%20Runtime语义MVP实现总结.md)。

- 建立最小 Journal、Reducer、Step Commit 和内存 Dispatcher；
- 跑通 A/B/C 并行、B 先返回、Wait 或 Abandon/Cancel；
- 证明当前 Step 看不到冻结之后到达的 Event；
- 证明 Abandon 与 Cancel 的事实和行为不同；
- 删除 State 后重放得到相同 State、Turn 和 Trace；
- 重放不重新调用模型和工具。

### 14.2 Durable 切片

> 已于 2026-08-21 完成独立 MVP，见 [Agent Runtime Durable MVP 实现总结](Agent%20Runtime%20Durable%20MVP实现总结.md)。

- 重复 delivery 不产生第二个决策 Event；
- 两个 Worker 竞争同一决策 Event 时只有一个 Step 能 Commit；
- Command 不会在 Step Commit 前执行；
- 并行结果按确定的决策消费顺序处理；
- `pending` 可以安全重新调度；
- `unknown` 不会触发契约外重试；
- `running` 可以依靠外部 receipt 查询或等待；
- Checkpoint 删除后仍可恢复同一逻辑 State。

### 14.3 边界切片

> 已于 2026-08-21 完成独立切片，见 [Agent Runtime 边界切片实现总结](Agent%20Runtime边界切片实现总结.md)。本切片按实现原则留白 Completion / Termination：Interrupt 只验收不被旧 Step 跨越或吞掉。该留白已由 [终态与 Finalization Barrier](Agent%20Runtime终态与Finalization%20Barrier实现总结.md) 补上。

- 未授权 Command 不会被 Dispatcher 认领；
- Step 决策期间到达的 Interrupt 不会被旧 Step 跨越或吞掉；
- 已 Commit 的用户输出通过 Command 投递，临时流式预览不进入 Journal；
- Artifact 缺失时重放明确降级，不使用当前内容静默替换；
- Turn 和 Trace 可以只依赖 Journal、Artifact 与 Projection 代码重建。

完成语义切片后即可重新评估接口形状，不必等待三次切片全部完成才获得反馈。完成三次验收后，再决定 Checkpoint 频率、Stream 与产品领域对象的映射，以及旧 HelperMe 的迁移顺序。

## 15. 最终边界

```text
Event       管理发生过什么
State       管理这些事实当前意味着什么
Step        管理一次模型决策闭环
Command     管理一次外部副作用闭环
Projection  管理不同消费者如何观察事实
Turn        管理人类如何理解一次交互
```

最终结论：

> **Agent 是由有序 Event 驱动的状态机；State 决定是否可以推进；Step 在冻结的决策状态上完成一次 LLM 决策并提交 Commands；Dispatcher 执行外部副作用并回写 Event；Turn 只是事件流上的人类交互视图。**
