# Agent Runtime 边界切片实现总结

> 状态：14.3 边界切片已完成（2026-08-21）  
> 权威设计：[Agent Runtime 状态推进模型](Agent%20Runtime状态推进模型.md)  
> 前置切片：[Agent Runtime 语义 MVP 实现总结](Agent%20Runtime语义MVP实现总结.md)、[Agent Runtime Durable MVP 实现总结](Agent%20Runtime%20Durable%20MVP实现总结.md)

## 1. 本次结论

`agent_runtime/` 已补上四条边界不变量，没有新增领域状态机，也没有实现 `COMPLETED` / `TERMINATED`。

Runtime 只裁决一件事：**这件事能不能发生。** 批准、取消、输出内容和 Artifact 是否还能业务替代，都不进入 Core。

```text
未授权 Command     → Dispatcher / Journal 不得认领
决策期间的 Interrupt → 旧 Step 不得跨越或吞掉
模型话术            → 不是已投递用户消息
流式预览            → 不能进入 Journal
缺失 Artifact       → 精确重放降级，Turn / Trace / State 仍可从 Journal 重建
```

## 2. 实现结构

14.1 / 14.2 模块继续保留；本次主要新增或收紧：

```text
agent_runtime/
├── events.py          CommandAuthorized / CommandRejected
├── artifacts.py       ArtifactRef、Store 与缺失降级
├── model.py           Command.requires_authorization
├── dispatcher.py      未授权 / 已拒绝 Command 不进入认领
├── journal.py         grant/reject CAS；无 eligibility 不得 start_attempt
├── sqlite_journal.py  同一套授权门与 command_rejections
├── state.py           授权事实归约；Rejected 可作为下一决策输入
├── projections.py     Replay 附带 ArtifactResolution
└── runtime.py         grant_command / reject_command 只写入事实
```

`STATE_CODEC_VERSION` 升到 2，因为 Command / CommandState 增加了授权字段。旧 Checkpoint 版本不匹配时退回完整 Journal replay。

## 3. 已闭合的边界

### 3.1 授权门

`issued` 不等于 `dispatch_eligible`。

- `requires_authorization=False`：`StepCommitted` 仍是派发资格证明。
- `requires_authorization=True`：Command 保持 pending，直到 Journal 接纳 `CommandAuthorized`。
- `CommandRejected` 使 Command 永远不可派发；后续 `grant` 返回 `None`。
- Journal `start_attempt` 在 `dispatch_eligible_event_id is None` 时直接拒绝，包括 `causation_id=None` 的伪造尝试。
- Runtime 不解释为什么批准或拒绝；它只检查有没有授权事实。

Rejected 是决策输入，不是 Runtime 终态。Agent 可以继续做下一 Step；是否停止由模型或外层决定。

### 3.2 Interrupt 不被跨越

旧 Step 在模型调用期间到达的 Interrupt：

- 不进入该 Step 的冻结 Decision State；
- 不能成为该 Step 的 trigger；
- 旧 Step Commit 之后仍是 `next_trigger`，Status 为 `RUNNABLE`。

本次不验收 Finalization，也不把 Interrupt 写成 Completion 竞争。

### 3.3 输出与预览

`ModelDecision.content` 只是决策记录，不会变成 `UserMessageReceived`。对用户的可靠投递必须是普通 Command，并留下 Outcome。Journal 没有预览 Event 类型；非法 payload 不能构造成 Event。

Runtime 不理解输出内容含义，因此 Turn 不会按「这句话像不像完成」去过滤。

### 3.4 Artifact 缺失

`MemoryArtifactStore.get` 只接受 `ArtifactRef`，缺失返回 `None`，不会去读工作区同名文件。`replay(..., artifact_store=store)` 在缺失时：

- `artifacts.complete is False`；
- Turn / Trace / Canonical State 仍从 Journal 重建。

这区分了精确上下文重放和运行恢复：缺正文不等于 Stream 作废。

## 4. 测试

边界测试位于：

- `tests/agent_runtime/test_boundary_slice.py`
- 既有 `test_semantic_slice.py` / `test_durable_slice.py`

覆盖：

- 未授权 Command 在 grant 前不被认领，伪造 `causation_id=None` 无效；
- reject 后不能 grant、不能派发，下一 Step 能看见拒绝事实；
- Interrupt 不被旧 Step 跨越或吞掉；
- 决策话术不是已投递用户消息；
- 预览 token 不能进入 Journal；
- Artifact 缺失明确降级，工作区文件不能顶替；
- SQLite 上两个 Worker 竞争 grant 只有一个成功，重启后资格仍在。

验证命令：

```text
python -m unittest tests.test_agent_runtime_suite
python -m unittest
```

最终结果：Agent Runtime 专项 59 项全部通过；全仓回归 516 项通过，2 项按原条件跳过。

## 5. 明确后置

本次继续留白，不进入当前切片：

- `COMPLETED` / `TERMINATED` 与 Finalization Barrier；
- Approval 工作流、授权策略和「为什么批准」；
- 完整 Model Context Replay Manifest / 请求 Artifact；
- Turn 与产品对话对象的正式映射；
- 旧 HelperMe 的 Adapter、迁移、灰度与最终替换。

14.3 的结论是：边界不变量可以从现有 Event / State / Step / Command 长出；授权是派发门，Interrupt 是不可跨越的决策输入，投递是 Command，Artifact 缺失是重放能力降级。
