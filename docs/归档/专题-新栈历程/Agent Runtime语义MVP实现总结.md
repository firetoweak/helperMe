# Agent Runtime 语义 MVP 实现总结

> 状态：14.1 语义切片已完成（2026-08-21）  
> 权威设计：[Agent Runtime 状态推进模型](Agent%20Runtime状态推进模型.md)
> 后续实现：[Agent Runtime Durable MVP 实现总结](Agent%20Runtime%20Durable%20MVP实现总结.md)

## 1. 本次结论

新 Runtime 已在同一仓库的独立 `agent_runtime/` 包内完成最小垂直切片，并保持对旧 `core.*` 的零依赖。

本次只验证新内核语义，不迁移旧 HelperMe，不双写新旧 Runtime，也不以兼容旧 `TurnRuntime` 为设计目标。

已落地的核心关系是：

```text
Event       唯一执行事实与顺序权威
State       Event Stream 的纯归约结果
Step        一次冻结决策与 Commands 的原子提交
Command     外部副作用及 Attempt / Outcome 闭环
Turn        面向人的可删除、可重建投影
```

## 2. 实现结构

```text
agent_runtime/
├── events.py        Event envelope 与最小事件集合
├── journal.py       Journal 协议与 MemoryJournal
├── model.py         Step、Command、Outcome、State 值对象
├── state.py         确定性 Reducer 与 DecisionFrame
├── step.py          冻结决策、Step 校验与原子 Commit
├── dispatcher.py    并行 Effect 执行与 Outcome 回写
├── projections.py   Turn、Trace、Replay 纯投影
└── runtime.py       外部事件接入与单进程推进门面
```

旧系统只会在未来迁移 Adapter 中被引用；`agent_runtime` 内部不会 import `core.*`。

## 3. 已验证语义

主场景已经跑通：

```text
UserMessage
→ Step 1 原子提交 A / B / C
→ Dispatcher 真并行执行
→ B 先完成
→ Step 2 只看见 B 的冻结状态
→ LLM 选择继续等待，或 Abandon 并请求 Cancel A / C
→ A / C 的迟到 Outcome 仍进入 Journal
→ State / Turn / Trace 可从全新 Journal 实例重建
```

关键约束已经进入代码，而不是留在约定中：

- Event 使用 Journal 接纳顺序；模型严格逐个消费可决策 Event。
- 决策期间到达的 A Outcome 或 Interrupt 不会泄漏进当前冻结 State。
- Step Commit 是包含 Decision 与全部 Commands 的单个 Event。
- Step 的 trigger、cursor、basis version、observed position 和 effect 对应关系可重放校验。
- Dispatch Attempt 与 Outcome 的直接因果引用可重放校验。
- Abandon 只改变 Agent 是否继续消费结果；它不删除事实，也不等于 Cancel。
- 等待者超时只停止观察，不会反向取消 Command。
- 本地协程取消不自动代表外部副作用取消；只有显式声明确认语义的 Tool Binding 才能产生 `CANCELLED`。
- 未声明取消能力的敏感工具保持 `UNKNOWN`，Cancel Outcome 为 `cancellation_unsupported`，不会制造虚假成功。
- Event 和 JSON 值深层不可变且载荷有界；超大工具错误保留原异常抛出，同时 Journal 只记录有界诊断。
- 未支持的 Event schema、伪造因果、重复稳定身份和不合法恢复 envelope 会在 Replay 时直接失败。
- Turn 保留 UserMessage 与 Interrupt 的 Event 身份；Replay 不调用模型和工具。

## 4. 当前状态语义

语义切片当前实际产生：

- `RUNNABLE`：存在一个按顺序待消费的决策 Event；
- `WAITING`：等待未完成 Command，或等待新的用户消息。

`COMPLETED`、`TERMINATED`、最终响应投递和 Interrupt 与 Finalization 的竞争属于后续边界切片，本次没有用隐藏启发式提前实现。

首次 Dispatch 在没有外部 receipt 时进入 `UNKNOWN`。`RUNNING` 只会在后续 Recovery Contract 能证明外部系统已接收后使用。

## 5. 测试

语义测试位于：

- `tests/agent_runtime/test_semantic_slice.py`
- `tests/test_agent_runtime_suite.py`

覆盖范围包括：

- A / B / C 真并行与 B first；
- Wait、Abandon、Cancel 及迟到 Outcome；
- 严格 Event 消费顺序与冻结视图；
- Outcome Commit / Cancel、等待超时等并发竞态；
- 不支持取消的敏感副作用；
- State、Turn、Trace 纯 Replay；
- Event 大小、恢复 envelope、schema、因果和稳定身份不变量。

验证命令：

```text
python -m unittest tests.test_agent_runtime_suite
python -m unittest tests.test_agent_runtime_suite tests.test_core_suite tests.test_plugin_suite
```

最终结果：语义套件 16 项全部通过；连同旧 Core 与 Plugin 的全量回归共 414 项通过，2 项按原条件跳过。

## 6. 明确后置

以下内容不属于 14.1，不继续塞入当前 MVP：

- 持久化 Journal 与 Checkpoint；
- delivery identity 与外部 Event 去重；
- 多 Worker 的 Step / Attempt 原子 Claim；
- `pending / unknown / running` 的恢复协议与外部 receipt；
- Command 授权快照；
- Artifact Store 与精确 Context Replay Manifest；
- Turn 与产品交互对象的正式映射；
- 旧 HelperMe 的 Adapter、灰度切换与最终替换。

14.2 Durable 切片已经完成。它只补持久化、Claim、去重和恢复，没有改写本次验证的 Event / State / Step / Command / Turn 职责。14.3 边界切片也已完成，见 [Agent Runtime 边界切片实现总结](Agent%20Runtime边界切片实现总结.md)。
