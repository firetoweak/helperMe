## Phase 5.1 Context Projection 总结

我们完成了模型上下文投影的最小闭环。

### 核心边界

- `Conversation`：Session 持有的完整协议轨迹，保存真实的 user、assistant、tool 消息。
- `ContextRequest`：描述本轮投影输入，包括 Conversation 消息和运行时指令。
- `ModelContext`：仅供当前一次模型请求使用的快照。
- `ContextManager`：唯一的上下文组装入口，不拥有 Session、Plan 或 system prompt。

运行时计划只进入 ModelContext，不写回 Conversation，因此不会污染后续 Run。

### 生命周期

```text
Session：长期聊天容器
└─ Conversation：跨 Run 保留
   └─ Run：一次用户输入的执行
      └─ Round：一次模型响应或工具循环
         └─ Retry：同一次模型请求的瞬时重试
```

由此确定：

- 每个 Round 重新生成 ModelContext。
- 同一个 Round 的所有 Retry 复用同一快照。
- Retry 期间不重新读取 Conversation、不 replan、不重新投影。
- 下一 Round 才能读取工具结果和最新 Plan 状态。

### 职责迁移

`RuntimeMode` 不再组装 messages，只提供：

```python
runtime_instructions() -> list[str]
```

- `PlainMode` 返回空列表。
- `PlanningMode` 返回当前计划文本。
- `ContextManager` 统一把这些指令注入 system 快照。
- 旧的 `prepare_messages()` 和 `build_runtime_messages()` 已完全删除。

### 依赖关系

Composition Root 创建并注入无状态的 `ContextManager`：

```text
Composition Root
    ↓
RunRuntime
    ├─ RuntimeMode：提供当前 Run 指令
    └─ ContextManager：构建当前 Round 快照
```

ContextManager 可以被多个 Session 共用，因为 Session 数据通过每次 `ContextRequest` 传入，并不保存在 ContextManager 内部。

### 验证结果

全量测试 `97/97` 通过，并明确覆盖：

- Conversation 不被快照修改。
- 运行时计划不污染 Conversation。
- 每个 Round 获取最新指令并重新投影。
- 同一 Round 的 Retry 复用同一个 messages 对象。
- 上下文超限不作为瞬时错误重试。
- PlainMode、PlanningMode、interrupt、Session 链路保持正常。

这一步最重要的认知是：

> Conversation 是事实，RuntimeMode 提供控制状态，ModelContext 是二者在某个 Round 上的临时投影。