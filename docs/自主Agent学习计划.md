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
这个测试提问不出错的前提下，能够在日志中清晰的观测到工具调用状态

ToolsState 是账本。ToolsRunner 是执行控制者，Checkpoint/RunResult 是对外报告。

基础的runtime已经有了。

抽出 ToolsRunner
短任务 runtime

定义 ToolsState
ToolsState.compact_completed() 目前只是状态层截断，还没有和 conversation.messages 的上下文压缩真正打通

工具链路检查
已经有了，toolsState 里

工具链路修复
还没有

Runner 退出结果

非持久化 Checkpoint
checkpoint 主要是run内报告，还不是可恢复执行点。

====================

Phase 2
Planning

====================

Task Decomposition

Execution Plan

Dynamic Replan

Reflection

Execution Monitoring

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