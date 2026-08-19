## Phase 1 · Phase 3 回顾补强记录（2026.07.14）

设计 SessionRuntime 时，上层对 interrupt、resume 和安全停止的需求暴露出 Phase 1 初版边界不清：ToolsState 混入 messages 协议处理，Checkpoint 混入停止安全判断，ToolStep 重复保存 result/ok/code/error，TurnResult 使用含糊的 terminated，并且缺少供上层请求安全中断的控制入口。

本次只为 Phase 3 回补 Tools Runtime，不扩展调度、持久化、Context/Facts 或多 Agent：

- ToolsState：仅保存一次 Turn 内的工具步骤账本；ToolStep.result 是唯一结果源，ok/code/error 改为派生属性；每个 call_id 只能记录一次 result。
- ToolsProtocol：独立负责 assistant tool_calls 与 tool results 的消息链校验及 tool message 转换。
- StopGuard：独立判断 protocol_safe 和 business_safe；只有消息链完整，并且最后一次成功写入后完成 get_changes，才允许 completed/interrupted。
- Checkpoint：只记录 Turn 内观察点，不再计算安全规则；Session 层生命周期记录统一称为 Event。
- TurnResult：使用 completed/interrupted/blocked/failed 四种 TurnStatus；final_reason 从最终 Checkpoint 派生，不重复保存 error。
- TurnControl：提供 interrupt_requested 控制信号；TurnRuntime 只在完整 tool batch 和业务安全点返回 interrupted。
- TurnRuntime：只负责编排模型调用、工具执行、协议、安全、Checkpoint 和统一 TurnResult 出口；ToolsState 不向 SessionRuntime 泄漏。

回补后的职责关系：

```text
TurnRuntime
├─ ToolsState：工具账本
├─ ToolsProtocol：消息协议
├─ ToolsExecutor：工具执行
├─ StopGuard：停止安全
└─ Checkpoint：Turn 内观测

TurnRuntime -> TurnResult -> SessionRuntime
```

验证：完整测试 41 项通过；未验证写入不能完成或安全中断，中断后的 tool_call/result 消息链保持合法。
