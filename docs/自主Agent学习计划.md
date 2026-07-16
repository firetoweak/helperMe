学习计划顺序文档。

每章节 初次只做核心原型，在后续章节做的时候，发现需要继续补充前边的章节的技术的时候，就进行回顾补充。
如果新 Phase 暴露出旧 Phase 的不足，就回补旧模块。但回补只服务当前 Phase，不做大而全重构。

## Rule 同步区

- 禁止为了跑通当前局部模块而添加静默兜底、隐式默认值或自动生成关键关联数据。兜底不得掩盖上层调用错误，否则会破坏整体设计并显著增加调试成本。

====================

Phase 0
Agent Core
====================
目标是做一个能够调用工具，并且能够读写文件的agent最小MCP。

学习内容：
1. 最小agent loop
2. 消息拼接格式
3. 调用工具的openAI api怎么定义的
4. 工具是如何注册和描述
5. 读写文件工具要做哪些？

Benchmark：
提问：你觉得项目的工具描述是不是有点像一个code agent？你帮我优化一下描述，让它更像一个通用智能体。
这个测试提问，agent能完成执行，并不出错（已达成 2026.06.30）
修改正确✓ 不死循环✓ get_changes一致✓ 最终总结真实✓ 

✓ OpenAI Protocol
简单的openAI api 
有：标准调用格式，支持配置api 和工具调用
缺：无流式

✓ Message
有：基础状态，消息拼装
缺：无trace跟踪

✓ Tool Registry
有：工具注册表

✓ Tool Execute
有：ok code data...输出标准格式

✓ Loop
有：完整一轮对话工具调用循环，system_prompt
缺：没有runtime

✓ Workspace
有：基础工作空间，用根目录形式搞的，
缺：没有做真正的沙箱拦截

✓ Verification
有：git diff 只能看到相比上次提交前是否改动此文件
缺：以后优化成真正的改动地方检测

====================

Phase 1
Reliable Tool-Calling Runtime

为什么做：当前可靠性主要依赖 system prompt 和模型自觉，所有agent运行状态都在一起了，需要拆解
====================
目标是 Phase 1 的目标不是做完整 Runtime，而是把当前 Agent.run 中混杂的 tool calling loop 抽象成可靠的 RunRuntime，并用 tools_state 管理当前 run 内的工具调用链路，使工具调用过程可检查、可修复、可截断、可停止。

学习内容：

Benchmark：
提问：
你觉得项目的工具描述是不是有点像一个 code agent？你帮我优化一下描述，让它更像一个通用智能体。
这个测试提问不出错的前提下，能够在日志中清晰的观测到工具调用状态（已完成）


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

### Phase 3 回顾补强记录（2026.07.14）

设计 SessionRuntime 时，上层对 interrupt、resume 和安全停止的需求暴露出 Phase 1 初版边界不清：ToolsState 混入 messages 协议处理，Checkpoint 混入停止安全判断，ToolStep 重复保存 result/ok/code/error，RunResult 使用含糊的 terminated，并且缺少供上层请求安全中断的控制入口。

本次只为 Phase 3 回补 Tools Runtime，不扩展调度、持久化、Context/Facts 或多 Agent：

- ToolsState：仅保存一次 run 内的工具步骤账本；ToolStep.result 是唯一结果源，ok/code/error 改为派生属性；每个 call_id 只能记录一次 result。
- ToolsProtocol：独立负责 assistant tool_calls 与 tool results 的消息链校验及 tool message 转换。
- StopGuard：独立判断 protocol_safe 和 business_safe；只有消息链完整，并且最后一次成功写入后完成 get_changes，才允许 completed/interrupted。
- Checkpoint：只记录 run 内观察点，不再计算安全规则；Session 层生命周期记录统一称为 Event。
- RunResult：使用 completed/interrupted/blocked/failed 四种 RunStatus；final_reason 从最终 Checkpoint 派生，不重复保存 error。
- RunControl：提供 interrupt_requested 控制信号；RunRuntime 只在完整 tool batch 和业务安全点返回 interrupted。
- RunRuntime：只负责编排模型调用、工具执行、协议、安全、Checkpoint 和统一 RunResult 出口；ToolsState 不向 SessionRuntime 泄漏。

回补后的职责关系：

```text
RunRuntime
├─ ToolsState：工具账本
├─ ToolsProtocol：消息协议
├─ ToolsExecutor：工具执行
├─ StopGuard：停止安全
└─ Checkpoint：run 内观测

RunRuntime -> RunResult -> SessionRuntime
```

验证：完整测试 41 项通过；未验证写入不能完成或安全中断，中断后的 tool_call/result 消息链保持合法。

====================

Phase 2
Planning

「让模型有显式计划」，不是做任务调度系统
给模型看的plan文本 + 轻量结构化外壳。plan 是“行动前的认知脚手架”，不是“可恢复执行状态”
为什么做： 当前 agent 执行多步骤任务时，容易直接进入工具调用，缺少显式任务分解和执行进度判断，导致跑偏、漏步骤或过早总结。

**注意** 当前 planning 涉及针对单个run 不涉及多个run组成的task (7.15)
====================

目标：让 agent 在执行任务前形成短计划，并在执行过程中根据工具结果更新计划判断。
计划主要服务模型推理，不追求持久化、恢复执行或复杂调度。

Benchmark：
面对一个需要读文件、分析、修改、验证的任务，agent 能先生成短计划；
执行过程中能在日志/checkpoint 中看到当前计划进度；（完成）
工具失败或信息不足时能更新计划；（不行×）
最终回答前能检查计划是否完成。（完成）


✓ Task Decomposition
模型自主把用户请求拆成少量意图阶段.
定义好固定的plan state，plan不仅仅是一段提示也是有状态的。

✓ Execution Plan
把计划注入模型上下文

✓ Execution Monitoring
工具调用后观察当前计划是否推进，根据plan state

？ Dynamic Replan
失败、信息不足、目标变化时改计划，计划的修改，好像有点问题。
我觉得可能是设计的问题，当前是start-text-tools-texts-end，如果修改计划，是需要修改plan后续状态的，当前只做到一次性的plan

✓ Reflection
最终回答前检查计划是否完成

✓ 补强 phase 1 checkpoint：
✓ - checkpoint 增加 plan 相关观测字段
✓ - run trace 中记录计划变化


遗留问题：
用户 **只读/禁止修改** 约束跟随，当前的agent并不能很好的跟随。
更稳定的工具失败动态重规划测试。

====================

Phase 3
Long-running Agent

把一次性 Agent.run 升级成可中断、可继续、可被人类介入的 Session Runtime。

====================

Benchmark：
一个多步骤任务开始后，系统能创建 session；
执行到安全点时可以 interrupt；
interrupt 后 messages/tool_call 链路仍然合法；
用户追加新的 user_message 后可以 resume；
resume 后 agent 能基于原 conversation 继续完成任务；
日志/Event 能看到 session: running -> interrupted -> running -> completed。


Session
定义好会话状态，一个多步任务/多轮交互的状态。
持久化到文件中，先搁置。

Session 必须持有：
1. conversation：恢复上下文
2. status：运行状态
3. event：可观察历史
4. run_records：历次 run 的最小摘要


重点：
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

Run 摘要边界：
- `SessionRunRecord` 只记录 run_id、状态、起止时间、结束原因等最小索引信息；
- verification 是 RunRuntime 内部的安全检查与 checkpoint/trace 观测数据，不复制到 `RunResult` 或 `SessionRunRecord`；
- RunRuntime 保证只有处于业务安全点的 run 才能 completed/interrupted；SessionRuntime 只根据最终 status/final_reason 驱动 Session 状态迁移；
- 需要验证细节时，通过 run_id 查询 run trace，避免跨层重复保存快照。


Context 边界
本阶段不提炼或保存 facts、constraints、feedback 分类：工具结果和新增 user_message 留在 conversation/run trace。分类、提炼和长期约束需要真实消费者，统一留到 Phase 5。


Interrupt
- 运行控制状态，不是具体业务策略
- 可恢复的中断点：Agent 执行到某个关键节点时，主动暂停，把当前状态交给外部系统或用户，等外部输入后再从原位置继续执行。
- Interrupt 不能只依赖 tool_call 链路完整，还要检查业务安全点。
写入类工具成功后，必须完成 get_changes，才允许进入 interrupted/completed。

Resume
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

====================


Phase 4
Agent Application Layer
====================

为什么做：Phase 3 后旧 Agent 仍绑定单个 Session，并混合依赖创建、Prompt、用例编排和日志职责，不利于多个入口复用。

目标：建立无状态 `AgentApplication`。Console/API 持有 session_id，应用层通过显式用例操作 SessionRuntime；不改变 Run/Session 语义。

学习内容：
1. Application Service 与显式用例。
2. Composition Root 与依赖注入。
3. Channel State、Prompt、Observability 边界。

职责关系：

```text
Console / API -> AgentApplication -> SessionRuntime -> RunRuntime
                    ↑
             Composition Root

Observability <- SessionRunOutcome
```

✓ AgentApplication：提供 create_session、start、resume、request_interrupt；不持有当前 Session、conversation 或 last_result。

✓ Composition Root：统一组装 LLMClient、RunRuntime、SessionRuntime、Prompt 和 AgentApplication。

✓ Console：持有 session_id/run_id，根据上次 RunStatus 显式选择 start 或 resume。

✓ Prompt：从应用服务中拆出，由组合入口选择并注入；以后可扩展为外部人格配置。

✓ Observability：只消费 SessionRunOutcome，不为日志或展示向 SessionRuntime 增加查询接口。

✓ 删除旧 core/agent.py，不保留第二套正式 API。

约束：
- 自下而上扩展；不因上层展示需求修改 SessionRuntime 以下的边界。
- 不引入 AgentCommand、Context/Memory、持久化 RunState、revision、async、调度、插件系统或 Event Bus。
- start 不隐式创建 Session，错误 session_id/run_id 立即失败。

Benchmark：
- 同一个 AgentApplication 可操作两个 Session，conversation 不串线。
- AgentApplication 不直接创建 LLMClient、RunRuntime，不包含 Prompt 常量和日志写入。
- Console 保持同 Session 多轮与 interrupt/resume。
- Phase 3/4 全量 91 项测试通过。

====================


Phase 5
Context Management
====================

5.1 Context Projection

5.2 Context Budget

5.2.1 Tool Result Budget / Runtime Artifact（5.3 前置）

5.3 Safe Compression

5.4 Memory 提炼

5.5 Retrieval

5.6 Workspace 上下文接入

====================


Phase 6
Goal Management
====================

Goal

Scheduler

Background Task

Watcher

Event

====================


Phase 7
Multi-Agent（最后）
====================

Coordinator

Worker

Delegation

Shared State

====================
