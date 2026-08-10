# Phase 1 / Phase 4 回补：阶段性说明

## 为什么回补

模型单轮可以同时返回 assistant `content` 与 `tool_calls`。旧协议把二者建模成互斥响应，导致工具调用同轮的阶段性说明被 Client 丢弃，Conversation 只能保存 `content=None`。

本次不新增“阶段解释层”。阶段性说明仍是模型原始 assistant content，不是 Context、Checkpoint、规划事件或隐藏思维链。

## 最终设计

```text
LLM assistant content + tool_calls
        ↓
LLMClient 保留混合响应
        ↓
Conversation 保存完整 assistant message
        ↓
RunRuntime 在工具执行前向 RunProgressSink 输出 content
        ↓
工具结果进入 Conversation，下一轮继续判断
```

- `LLMResponse` 收敛为 `content: str` 与不可变 `calls: tuple[ToolCall, ...]`，不再保留互斥 `type`。
- 判断顺序固定为：有 `calls` 就执行工具；否则非空 `content` 才是最终回答候选；二者都没有则是非法空响应。
- `RunProgressSink` 是 Runtime 对外输出端口；无消费者时使用 Null Object，Console 绑定打印实现。
- 只输出主 Agent Loop 中携带工具调用的 content。Router、Todo 初始化等内部受限调用不进入 Conversation，也不输出给用户。
- Sink 异常不捕获、不包装，内部继续相信端口契约。

## Prompt 约束

调用实质性工具前，用一到两句话说明当前判断和动作，并在同一响应立即调用工具；路线调整时简短说明原因；不展示隐藏推理，不播报显而易见的小操作。

这是轻约束：模型仍可能合法返回只有 `tool_calls` 的响应。Runtime 负责不丢内容和及时输出，不伪造模型没有生成的说明。

## 验证

- 单元测试覆盖混合响应解析、Conversation 完整保存、说明先于工具执行，以及纯文本最终回答不进入进度端口。
- 六轮真实 Session 验证的阶段性说明数量为 `1、4、0、1、0、0`；工具型分析出现说明，普通承接回复没有伪造说明。

结论：HelperMe 缺失的是混合响应协议与进度输出端口，而不是新的规划或阶段管理机制。
