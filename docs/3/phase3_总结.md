## Phase 3 · Long-running Agent 总结

把一次性 Agent.run 升级成可中断、可继续、可被人类介入的 Session Runtime。

### Benchmark

一个多步骤任务开始后，系统能创建 session；
执行到安全点时可以 interrupt；
interrupt 后 messages/tool_call 链路仍然合法；
用户追加新的 user_message 后可以 resume；
resume 后 agent 能基于原 conversation 继续完成任务；
日志/Event 能看到 session: running -> interrupted -> running -> completed。

### Session

定义好会话状态，一个多步任务/多轮交互的状态。
持久化到文件中，先搁置。

Session 必须持有：

1. conversation：恢复上下文
2. status：运行状态
3. event：可观察历史
4. run_records：历次 run 的最小摘要

### 重点

- conversation 是协议层消息历史；ToolsState 是 runtime 层工具账本。它们互相映射，但不是包含关系。
- facts 推迟到 Phase 5 Context Management：Phase 3 复用完整 conversation，没有独立 facts 的实际消费者，不提前复制工具结果或对话摘要。
- constraints 推迟到 Phase 5：Phase 3 将 resume 输入视为新的 user_message，不判断它是继续指令、反馈还是长期约束。没有约束消费者时，提前分类只会复制数据并制造同步责任。
- 不保存 progress.last_safe_point：RunRuntime 已保证 completed/interrupted 只发生在安全点，SessionRuntime 基于完整 conversation 继续即可。没有持久化恢复消费者时，再保存一份恢复位置属于重复状态。
- session events 应该分层，不直接包含 tools runtime 的全部 events，只保存 session 层事件和 run 摘要。工具 runtime 的完整 event 留在 run result / run trace。
- Session Event 在本阶段只记录生命周期，事件均由 SessionRuntime 产生，因此不设计 event source。等出现真实的多来源事件消费者后再引入来源模型。
- Session Event 不提供任意 data 字典；当前生命周期字段已明确，提前开放无约束扩展口会弱化事件契约。

✓ 回补 Phase 1 Tools Runtime：
SessionRuntime 设计暴露出 ToolsState、协议校验、停止安全和结果状态边界不清；
已按 Phase 3 的 interrupt/resume 需求完成职责拆分，不扩展无关能力。

### Run 摘要边界

- `SessionRunRecord` 只记录 run_id、状态、起止时间、结束原因等最小索引信息；
- verification 是 RunRuntime 内部的安全检查与 checkpoint/trace 观测数据，不复制到 `RunResult` 或 `SessionRunRecord`；
- RunRuntime 保证只有处于业务安全点的 run 才能 completed/interrupted；SessionRuntime 只根据最终 status/final_reason 驱动 Session 状态迁移；
- 需要验证细节时，通过 run_id 查询 run trace，避免跨层重复保存快照。

### Context 边界

本阶段不提炼或保存 facts、constraints、feedback 分类：工具结果和新增 user_message 留在 conversation/run trace。Phase 5 最终选择 `Conversation + ContextState + ModelContext` 的安全投影方案，没有复制一套 facts/constraints；长期 Memory 继续后置，避免没有消费者时提前建模。

### Interrupt

- 运行控制状态，不是具体业务策略
- 可恢复的中断点：Agent 执行到某个关键节点时，主动暂停，把当前状态交给外部系统或用户，等外部输入后再从原位置继续执行。
- Interrupt 不能只依赖 tool_call 链路完整，还要检查业务安全点。
写入类工具成功后，必须完成 get_changes，才允许进入 interrupted/completed。

### Resume

resume 不是崩溃恢复，也不是持久化恢复；
只是同一进程内，基于 session 状态继续执行。
resume 接收新的 user_message，但不判断或复制其语义；消息由 RunRuntime 写入原 conversation。
本阶段不设计 Task Queue；active_controls 只管理当前同步 run 的控制信号，不是调度队列。

✓ Agent 接入 SessionRuntime：

- Agent 不再直接调用或持有 RunRuntime；RunRuntime 由 SessionRuntime 编排。
- Agent 的 conversation 指向当前 Session 持有的 conversation，pending/completed 时 start，interrupted 时 resume。
- completed 表示当前 run 已完成并等待下一条 user_message；下一轮在同一 Session、同一 conversation 中重新进入 running。interrupted 才使用 resume；blocked、failed 仍是终态。
- SessionRuntime 使用临时 SessionRunOutcome 向调用方返回 RunResult 与 SessionRunRecord；Outcome 不写入 Session，避免长期状态重复。
- 跨层测试已验证 Agent -> SessionRuntime -> RunRuntime 的 interrupt/resume、conversation 协议完整性及完整 Session Event 流。
