学习计划顺序文档。

每章节 初次只做核心原型，在后续章节做的时候，发现需要继续补充前边的章节的技术的时候，就进行回顾补充。
如果新 Phase 暴露出旧 Phase 的不足，就回补旧模块。但回补只服务当前 Phase，不做大而全重构。

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
目标是 Phase 1 的目标不是做完整 Runtime，而是把当前 Agent.run 中混杂的 tool calling loop 抽象成可靠的 tools_runner，并用 tools_state 管理当前 run 内的工具调用链路，使工具调用过程可检查、可修复、可截断、可停止。

学习内容：

Benchmark：
提问：
你觉得项目的工具描述是不是有点像一个 code agent？你帮我优化一下描述，让它更像一个通用智能体。
这个测试提问不出错的前提下，能够在日志中清晰的观测到工具调用状态（已完成）


✓ 基础的runtime已经有了。
ToolsState 是账本。ToolsRunner 是执行控制者，Checkpoint/RunResult 是对外报告。


✓ 抽出 ToolsRunner，作为整个agent的心脏，最小运行内核
短任务 runtime，保持其生命周期在一轮text-tools-text，一次多轮的工具调用。不持久的化状态。
模型异常（完成了）

✓ 定义 ToolsState
ToolsState.compact_completed() ~~目前只是状态层截断，还没有和 conversation.messages 的上下文压缩真正打通~~
超上下文直接报错，本期不做上下文压缩截断这些上下文优化操作。

✓ 工具链路检查
已经有了，toolsState 里
工具链路修复，还没有

✓ Runner 退出结果

✓ 非持久化 Checkpoint
checkpoint 主要是run内报告，还不是可恢复执行点。

Phase 1 已完成最小可靠 tool-calling runtime：工具调用循环已从 Agent 中抽出，工具链状态可检查、可报告、可在异常/预算耗尽时安全停止。上下文压缩、恢复执行、长期会话不属于本阶段。

====================

Phase 2
Planning

「让模型有显式计划」，不是做任务调度系统
给模型看的plan文本 + 轻量结构化外壳。plan 是“行动前的认知脚手架”，不是“可恢复执行状态”
为什么做： 当前 agent 执行多步骤任务时，容易直接进入工具调用，缺少显式任务分解和执行进度判断，导致跑偏、漏步骤或过早总结。
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

Phase 2.5
Console Multi-turn Harness

目标：为 Phase 3 Session Runtime 提供最小多轮观察入口。
====================

把当前单轮 Agent.run 测试方式，补成一个同进程内的多轮控制台对话入口，用于观察 conversation 累积、session 状态流和 human feedback 注入。

Session 不是从 Agent 派生出来的；
Session 是 Runtime 管理的状态对象；
Agent 是 Runtime 用来推进 Session 的执行器。

只做：
- 启动时创建一个 session
- 多轮读取用户输入
- 每轮复用同一个 session/conversation
- 打印 assistant 输出
- 依赖现有日志观察状态变化

不做：
- 命令系统
- 多 session 切换
- 持久化恢复
- 后台任务
- 复杂 TUI

校验
✓ 第二轮输入时，模型“记得上一轮” （完成）


====================

Phase 3
Long-running Agent

把一次性 Agent.run 升级成可中断、可继续、可被人类介入的 Session Runtime。
====================

Benchmark：
一个多步骤任务开始后，系统能创建 session；
执行到安全点时可以 interrupt；
interrupt 后 messages/tool_call 链路仍然合法；
用户追加 human feedback 后可以 resume；
resume 后 agent 能基于原 conversation 继续完成任务；
日志/Event 能看到 session: running -> interrupted -> running -> completed。


Session
定义好会话状态，一个多步任务/多轮交互的状态。
持久化到文件中，先搁置。

Session 必须持有：
1. conversation：恢复上下文
2. status：运行状态
3. event：可观察历史
4. constraints：用户反馈/约束
5. progress.last_safe_point：恢复位置


重点：
- conversation 是协议层消息历史；ToolsState 是 runtime 层工具账本。它们互相映射，但不是包含关系。
- facts 推迟到 Phase 4 Context Management：Phase 3 复用完整 conversation，没有独立 facts 的实际消费者，不提前复制工具结果或对话摘要。
- session events 应该分层，不直接包含 tools runtime 的全部 events，只保存 session 层事件和 run 摘要。工具 runtime 的完整 event 留在 run result / run trace。

Run 摘要边界：
- `SessionRunRecord` 只记录 run_id、状态、起止时间、结束原因等最小索引信息；
- verification 是 ToolsRunner 内部的安全检查与 checkpoint/trace 观测数据，不复制到 `RunResult` 或 `SessionRunRecord`；
- ToolsRunner 保证只有处于业务安全点的 run 才能 completed/interrupted；SessionRuntime 只根据最终 status/final_reason 驱动 Session 状态迁移；
- 需要验证细节时，通过 run_id 查询 run trace，避免跨层重复保存快照。


facts
本阶段不提炼或保存 facts：工具结果留在 conversation/run trace；用户反馈按用途进入 conversation、event 或 constraints。


Interrupt
- 运行控制状态，不是具体业务策略
- 可恢复的中断点：Agent 执行到某个关键节点时，主动暂停，把当前状态交给外部系统或用户，等外部输入后再从原位置继续执行。
- Interrupt 不能只依赖 tool_call 链路完整，还要检查业务安全点。
写入类工具成功后，必须完成 get_changes，才允许进入 interrupted/completed。

Human Feedback
- 中断后，人类可以补充意见，然后 agent 继续。
- Runtime Feedback 与 Human Feedback 分离。
- Runtime Feedback 是运行器对模型的控制反馈，例如要求补齐验证；
- Human Feedback 是用户对目标或约束的反馈。
二者都可以进入 conversation，但在 session event 中必须保留来源。

Resume
resume 不是崩溃恢复，也不是持久化恢复；
只是同一进程内，基于 session 状态继续执行。

Task Queue
pending session
active session
先了解这俩状态

====================


Phase 4
Context Management
====================

Workspace

Conversation

Memory

Retrieval

Compression

====================


Phase 5
Goal Management
====================

Goal

Scheduler

Background Task

Watcher

Event

====================


Phase 6
Multi-Agent（最后）
====================

Coordinator

Worker

Delegation

Shared State

====================
