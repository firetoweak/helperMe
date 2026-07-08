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
执行过程中能在日志/checkpoint 中看到当前计划进度；
工具失败或信息不足时能更新计划；
最终回答前能检查计划是否完成。


✓ Task Decomposition
模型自主把用户请求拆成少量意图阶段

✓ Execution Plan
把计划注入模型上下文

✓ Execution Monitoring
工具调用后观察当前计划是否推进

？ Dynamic Replan
失败、信息不足、目标变化时改计划，计划的修改，好像有点问题。
我觉得可能是设计的问题，当前是start-text-tools-texts-end，如果修改计划，是需要修改plan后续状态的，当前只做到

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
====================

Session

Interrupt

Resume

Human Feedback

Task Queue

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