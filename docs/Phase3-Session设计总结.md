# Phase 3 Session Runtime 设计总结

Phase 3 的核心目标不是做后台任务系统，而是把一次性的 `Agent.run` 升级成可中断、可继续、可被人类介入的 `Session Runtime`。

一句话理解：

```text
长任务 agent 的关键不是跑得久，而是状态可观察、可暂停、可反馈、可继续。
```

## 1. Session 中几个关键状态

Session 不应该直接持有完整的 `ToolsState`。`ToolsState` 是一次 run 内部的工具执行账本，主要服务 runtime 检查 tool_call/result 链路是否合法。

Session 更关心长期任务状态：

```text
Session
- status
- conversation
- facts
- events/checkpoints
- run summaries
- constraints
- progress
```

### Conversation

`conversation` 是 OpenAI messages 协议层的消息历史。

它记录：

```text
- 用户输入
- assistant 回复
- assistant tool_calls
- tool result messages
- runtime feedback / human feedback 注入给模型的内容
```

它的职责是：

```text
让下一次 LLM 调用能基于合法 messages 继续执行。
```

它不是完整的 `ToolsState`，但和 `ToolsState` 有映射关系：

```text
conversation.messages 里的 assistant.tool_calls
    -> ToolsState.steps 里的 call_id/name/arguments

conversation.messages 里的 tool message
    -> ToolsState.steps 里的 result/ok/code/error
```

可以这样理解：

```text
conversation 是协议层消息历史；
ToolsState 是 runtime 层工具账本。
```

一个长 conversation 可以看成多个完整 runtime run 拼接而成：

```text
Session
  -> Run 1
      -> ToolBatch 1
      -> ToolBatch 2
      -> Final/Interrupt
  -> Run 2
      -> ToolBatch 1
      -> Final/Interrupt
```

后续 Phase 4 做上下文压缩时，conversation 可以被裁剪、压缩、重建，但必须保证截断点落在协议完整的位置。

最低要求：

```text
不能截断在 assistant(tool_calls) 之后、tool result 之前。
```

更安全的截断点：

```text
完整 tool batch 之后；
最好是完整 run 之后。
```

### Facts

`facts` 是为 Phase 4 Context Management 做铺垫的语义状态。

它不是完整工具输出，也不是 conversation 摘要全文。

它应该保存：

```text
从可信来源提炼出的、后续继续任务真正需要的最小事实。
```

不建议把完整工具输出直接塞进 facts。否则 facts 会变成第二份 conversation/tool log，后续仍然臃肿。

适合放入 facts：

```text
- 用户明确要求“不要修改文件”
- 已读取 core/agent.py
- 已定位 Agent.run 的实现位置
- get_changes 显示某文件发生了改动
- 写入后已经完成验证
```

不适合放入 facts：

```text
- 大段文件全文
- 完整 tool result JSON
- 模型主观判断：“代码整体还不错”
- 无来源的总结：“这个方案应该可行”
```

一个最小结构可以是：

```python
@dataclass
class SessionFact:
    kind: str
    data: dict
    source: str
    confidence: str = "verified"
```

关键点是 `source`。以后 conversation 被压缩后，facts 仍然需要能说明自己来自哪里：

```text
tool:file_read
tool:get_changes
human_feedback
runtime_feedback
```

判断标准：

```text
如果信息是为了复现工具调用细节，放 trace。
如果信息是为了满足 messages 协议，放 conversation。
如果信息是为了让 agent 压缩后还能继续理解任务，放 facts。
```

### Checkpoint / Event

Session 层的 checkpoint 不应该直接包含 ToolsRunner 的全部 checkpoints。

应该分层：

```text
ToolsRunner checkpoints:
记录一次 run 内部发生了什么。

Session events/checkpoints:
记录长期任务状态如何变化。
```

ToolsRunner checkpoint 适合记录：

```text
- run_started
- tool_batch_completed
- llm_retry
- message_chain_invalid
- context_length_exceeded
- runtime_feedback_injected
- run_completed
- max_rounds_exceeded
```

Session event/checkpoint 适合记录：

```text
- session_created
- session_started
- human_feedback_added
- runtime_feedback_added
- session_interrupted
- session_resumed
- session_completed
- session_blocked
```

建议中间加一层 run summary，连接 Session 和具体 run：

```python
@dataclass
class SessionRunRecord:
    run_id: str
    status: str
    started_at: str
    ended_at: str | None
    checkpoints_count: int
    final_reason: str | None
    verification: dict
    plan_snapshot: dict | None
```

边界判断：

```text
如果信息用于解释某一轮工具调用细节，放 runner checkpoint。
如果信息用于解释 session 生命周期变化，放 session event。
如果信息用于连接 session 与某次 run，放 run summary。
```

## 2. Interrupt 的状态设计

Interrupt 不是错误，而是 Session Runtime 的一种正常状态切换。

它表示：

```text
agent 在安全点暂停，把当前状态交给外部系统或用户，等待反馈后继续。
```

Phase 3 先不要做复杂业务策略。比如“删除文件前审批”“等待 subagent”都可以作为未来扩展。

当前阶段先关注：

```text
- 怎么停
- 停在哪里
- 停后 messages 是否仍然合法
- 停后是否满足业务安全
- 人类反馈后如何 resume
```

### 两种安全点

Interrupt 不能只看 tool_call 链路是否完整。

需要区分两种安全：

```text
ProtocolSafePoint:
messages 中没有未闭合的 tool_call。

BusinessSafePoint:
外部副作用已经完成必要验证。
```

例如：

```text
assistant(tool_calls)
```

这里不能中断，因为缺少 tool result。

```text
assistant(tool_calls)
-> tool result(s)
```

这里协议上安全，但不一定业务安全。

如果已经执行了写入工具，但还没有执行 `get_changes`，那么：

```text
protocol_safe = true
business_safe = false
```

这时不应该进入 interrupted/completed。

### 写入后的验证守门

当前 `get_changes` 主要依赖 prompt 控制，模型可能遗漏。

Phase 3 应该把它提升为 runtime 守门规则：

```text
写入类工具成功后，必须完成 get_changes，才允许 completed/interrupted。
```

但 runtime 不应该直接替模型调用 `get_changes`。

更好的边界是：

```text
模型负责行动；
runtime 负责检查状态、拒绝不安全停止，并把修正要求反馈给模型。
```

也就是：

```text
Runtime 不替模型完成业务动作；
Runtime 只检查能否安全停止，并在不安全时把修正要求反馈给模型。
```

流程：

```text
1. 模型调用 write_file/apply_patch
2. 工具成功
3. runtime 发现 needs_verification=true
4. 如果模型想 final answer，或用户请求 interrupt
5. runtime 不允许结束
6. 注入 runtime feedback，要求模型先调用 get_changes
7. 继续同一个 run
8. 验证完成后，再 completed 或 interrupted
```

### Runtime Feedback

`runtime feedback` 应该和 `human feedback` 分开。

```text
Human Feedback:
来自用户，表达目标、约束、纠错、审批。

Runtime Feedback:
来自运行器，表达协议、安全、预算、验证要求。

Tool Result:
来自工具，表达外部世界的执行结果。
```

三者都可以进入 conversation，但在 session event 中必须保留来源。

例如 runtime feedback：

```text
检测到写入类工具已经成功执行，但尚未调用 get_changes 验证。
在最终回答或中断前，必须先调用 get_changes。
```

runtime feedback 注入后，不应该立刻返回 interrupted/blocked。

它应该立即继续同一个 run：

```text
running
-> runtime_feedback_injected
-> running
-> completed / interrupted / blocked
```

`runtime_feedback_injected` 不需要成为 Session.status，它只是一次事件或 checkpoint。

### Interrupt 状态流

Session status 可以先保持简单：

```text
pending
running
interrupted
completed
blocked
failed
```

interrupt 请求出现后：

```text
interrupt_requested = true

如果 protocol_safe=false:
    不能中断，先等当前 tool batch 补齐结果。

如果 protocol_safe=true 且 business_safe=false:
    注入 runtime feedback，要求模型补齐验证。
    继续同一个 run。

如果 protocol_safe=true 且 business_safe=true:
    返回 interrupted。
```

如果注入 runtime feedback 后模型仍然不执行验证，或者达到 max_rounds：

```text
返回 blocked/terminated。
```

其中：

```text
interrupted:
正常暂停，等待 human feedback 或 resume。

blocked:
runtime 已经给出修正要求，但模型无法推进到安全状态。

failed:
系统错误、协议破坏、不可恢复异常。
```

### Resume

Phase 3 的 resume 不是崩溃恢复，也不是持久化恢复。

当前阶段只做：

```text
同一进程内，基于 session 状态继续执行。
```

resume 的基本流程：

```text
1. 检查 session.status == interrupted
2. 如果有 human feedback，追加到 conversation，并记录 session event
3. session.status = running
4. 调用 ToolsRunner 继续执行
5. 根据结果更新 session.status
```

## 3. Phase 3 的最小 Benchmark

```text
一个多步骤任务开始后：
1. 系统能创建 session；
2. 执行过程中能在安全点 interrupt；
3. interrupt 后 messages/tool_call 链路仍然合法；
4. 如果写入后未验证，不允许进入 interrupted/completed；
5. runtime feedback 会要求模型先 get_changes；
6. 用户追加 human feedback 后可以 resume；
7. resume 后 agent 能基于原 conversation 继续完成任务；
8. 日志/checkpoint 能看到 session 状态变化：
   running -> interrupted -> running -> completed。
```

## 4. 当前阶段暂不做

Phase 3 先不做这些：

```text
- 文件持久化恢复
- 崩溃后恢复
- 完整 task scheduler
- 并发队列
- 优先级调度
- 后台 watcher
- 多 agent/subagent 协作
- 复杂自动 facts 抽取
```

Task Queue 只学习最小概念：

```text
pending session
active session
```

不要提前进入 Phase 5。

