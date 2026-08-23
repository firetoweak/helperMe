# Agent Runtime 终态与 Finalization Barrier 实现总结

> 状态：Completion / Termination + Finalization Barrier 已完成（2026-08-21）  
> 权威设计：[Agent Runtime 状态推进模型](Agent%20Runtime状态推进模型.md)  
> 前置切片：[Agent Runtime 语义 MVP 实现总结](Agent%20Runtime语义MVP实现总结.md)、[Agent Runtime Durable MVP 实现总结](Agent%20Runtime%20Durable%20MVP实现总结.md)、[Agent Runtime 边界切片实现总结](Agent%20Runtime边界切片实现总结.md)

## 1. 本次结论

执行生命线现在有合法终点。Turn 结束仍是默认现象；Stream 终态是显式、稀有、有语义重量的事实。

```text
content-only                  → WAITING(user_message)
Step lifecycle_intent=complete/terminate
        ↓
Finalization Barrier
        ↓
RuntimeCompleted / RuntimeTerminated

TerminationRequested(user/host)
        ↓ 同一事务：作废 Step claim、Abandon 未终态 Command
        ↓
RuntimeTerminated
```

没有给 Runtime 增加 Goal、Todo、Approval 工作流或 Artifact Store。Barrier 只做确定性 CAS，不判断任务是否“真的完成”。

## 2. 实现结构

```text
agent_runtime/
├── model.py           LifecycleIntent；ModelDecision.lifecycle_intent
├── events.py          TerminationRequested / RuntimeCompleted / RuntimeTerminated
├── finalization.py    资格谓词与终态 Event 构造
├── state.py           终态归约；terminal 后不再 RUNNABLE
├── journal.py         finalize CAS；accept_termination 同事务
├── sqlite_journal.py  stream_terminals 唯一约束
└── runtime.py         finalize / receive_termination；advance 先收口
```

## 3. 已闭合的不变量

### 3.1 声明不是终态

`CompletionDeclared` / `TerminationDeclared` 是 `StepCommitted.decision.lifecycle_intent`，不是独立 Event。Barrier 成功后才追加 `RuntimeCompleted` / `RuntimeTerminated`。

content-only 不得推导为完成。Interrupt 仍是决策输入；旧 Step 的完成声明若碰上未消费 Interrupt，自动 stale，必须由新 Step 再声明。

### 3.2 资格可从 Journal 推导

不写 `FinalizationRejected`。一份声明 eligible，当且仅当：

- 还没有终态事实；
- 它是最新 Step 的意图，或一份未被更新用户意图覆盖的 `TerminationRequested`；
- Step 路径没有未消费决策 Event，也没有未放弃的 InvokeTool；
- `DispatchAttemptStarted` 等 operational noise 不让声明过期。

进程在 Step Commit 之后、Barrier 之前崩溃，且世界未出现 supersede 事实时，`recover_once` / `finalize` 继续同一次 Barrier。

### 3.3 Host / 用户停止是打断

`TerminationRequested` 带 `DeliveryIdentity`，不是 decision input。接纳后同一事务：

- 作废当前 Step claim，模型稍后返回也不能 Commit；
- 将未终态 Command 视为 abandoned；
- 写入 `RuntimeTerminated`。

Abandon 不等于 Cancel。本切片不自动签发 Cancel；迟到 Outcome 仍可写入，但不触发新 Step，也不能复活终态。

Step 自己声明 terminate/complete 时，飞行中的 InvokeTool 仍是必要依赖；Runtime 不替模型自动 Abandon，也不因“说了完成”而拒绝同时签发工具。Outcome 到来后需要新的 Step 再声明。

### 3.4 终态之后

普通 UserMessage / Interrupt 不能把 `COMPLETED` / `TERMINATED` 改回 `RUNNABLE`。新的交互 epoch 或 `RuntimeResumed` 后置。

## 4. 测试

测试位于 `tests/agent_runtime/test_finalization_slice.py`。

覆盖：

- content-only 等待下一句用户话；
- 显式 complete 成为 `COMPLETED`，replay 一致；
- 未放弃的飞行工具让 Step 终态声明无法兑现；
- 放弃后 complete / terminate 可以兑现，迟到 Outcome 不复活；
- Interrupt 让旧完成声明 stale；
- 未跑完的 Barrier 可在恢复后继续；
- `/stop` 作废 in-flight Step claim，并放弃已在跑的 Command；
- SQLite 上两个 Worker 竞争 finalize 只有一个终态事实。

验证命令：

```text
python -m unittest tests.agent_runtime.test_finalization_slice
python -m unittest tests.test_agent_runtime_suite
```

## 5. 明确后置

- Turn 与产品对话对象的正式映射；
- Approval 工作流（授权门已在 14.3）；
- Artifact Store 与 Context Replay Manifest；
- 旧 HelperMe Adapter 与同一真实任务对照；
- `RuntimeResumed` / 新 Stream epoch；
- Host stop 自动签发 Cancel。

下一步仍是收口工程的外围适配，而不是给 Runtime 增加 SubAgent、后台任务、Long Memory 或 Goal 新建模。
