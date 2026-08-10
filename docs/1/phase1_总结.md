## Phase 1 · Reliable Tool-Calling Runtime 总结

为什么做：当前可靠性主要依赖 system prompt 和模型自觉，所有agent运行状态都在一起了，需要拆解

目标是 Phase 1 的目标不是做完整 Runtime，而是把当前 Agent.run 中混杂的 tool calling loop 抽象成可靠的 RunRuntime，并用 tools_state 管理当前 run 内的工具调用链路，使工具调用过程可检查、可修复、可截断、可停止。

### 学习内容

### Benchmark

提问：
你觉得项目的工具描述是不是有点像一个 code agent？你帮我优化一下描述，让它更像一个通用智能体。
这个测试提问不出错的前提下，能够在日志中清晰的观测到工具调用状态（已完成）

### 模块状态

✓ 基础的runtime已经有了。

ToolsState 是账本。RunRuntime 是执行控制者，Checkpoint/RunResult 是对外报告。

✓ 抽出 RunRuntime，作为整个agent的心脏，最小运行内核

短任务 runtime，保持其生命周期在一轮text-tools-text，一次多轮的工具调用。不持久的化状态。
模型异常（完成了）

✓ 定义 ToolsState

ToolsState.compact_completed() ~~目前只是状态层截断，还没有和 conversation.messages 的上下文压缩真正打通~~
超上下文直接报错，本期不做上下文压缩截断这些上下文优化操作。

✓ 工具链路检查（初版）

初版放在 ToolsState 中；Phase 3 回顾时发现协议校验不属于账本职责，后续已拆出。

✓ Runner 退出结果

✓ 非持久化 Checkpoint

checkpoint 主要是run内报告，还不是可恢复执行点。

Phase 1 已完成最小可靠 tool-calling runtime：工具调用循环已从 Agent 中抽出，工具链状态可检查、可报告、可在异常/预算耗尽时安全停止。上下文压缩、恢复执行、长期会话不属于本阶段。

### Phase 6A 压力测试回补（2026.08.10）

跨 Run Goal 验收暴露出初版 `RunResult` 只适合报告运行状态，不能承载机器可验证的完成事实。本次回补：

- `RunResult` 增加 `RunEvidence` 快照；工具原始结果在外置或裁剪前写入证据账本，模型自由文本不作为验收事实。
- `RunInvocation / RunCapability` 成为 Run 级扩展入口，Capability 可以注入临时工具、运行说明、完成门禁，并声明是否允许基础工具。
- 临时 ToolRegistry 不再只解决“工具何时释放”，还解决“本 Run 有权使用哪些工具”；PlanRevision Run 因此无法调用文件或 Shell 工具。

这次回补确认：ToolsState 负责协议账本，RunEvidence 负责执行事实，RunResult 负责对外结果，三者不能合并。
