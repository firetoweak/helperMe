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
