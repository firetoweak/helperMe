## Phase 5.2 Context Budget 总结

我们完成了上下文预算的最小闭环：所有正式模型调用在发送前检查项目输入预算，成功后使用模型返回的真实 usage 校准估算并记录消费。

### 三种责任边界

- 模型硬限制：由模型提供方定义和最终执行，`LLMClient` 将超限错误映射为 `LLMContextLengthError`。
- 项目预算策略：由 `ContextBudget` 执行，当前规定完整模型输入最多占模型窗口的 75%。
- 消息协议安全：由 `ToolsProtocol` 定义，`ContextManager` 保证最终 ModelContext 的工具消息链合法。

模型硬限制是外部事实，75% 是项目决策，协议安全是任何上下文投影都必须满足的不变量。

### 预算对象

预算不作用于 Conversation，而作用于本次完整模型请求：

```text
完整模型输入
= ModelContext.messages
+ tools schema
```

`ModelContext.messages` 已包含：

- system prompt；
- Conversation 协议轨迹；
- 当前 user message；
- 当前 runtime instructions。

Conversation 即使超过模型窗口也仍然可以是合法、完整的事实轨迹。预算不足不能删除或修改 Conversation。

### 核心组件

```text
core/context/
├─ manager.py    上下文投影
├─ budget.py     预算配置、评估与策略
└─ estimator.py  输入规模估算与系数校准

core/model_call/
├─ client.py     外部模型 API
├─ types.py      响应、usage 和调用结果
└─ service.py    单次模型调用的统一入口
```

职责关系：

```text
ModelCallService
├─ ContextBudget：判断是否允许发送
└─ LLMClient：执行外部模型调用

ContextBudget
└─ TokenEstimator：估算完整输入大小
```

`BudgetAssessment` 只保存本次临时判断：

```text
estimated_input_tokens
input_budget_tokens
allowed               派生属性
overflow_tokens       派生属性
```

它不修改 ModelContext，也不决定 RunStatus。`overflow_tokens` 为 Phase 5.3 Safe Compression 保留了稳定接缝，但本阶段没有实现压缩或截断。

### 估算与校准

本阶段不绑定具体 tokenizer。`TemplateTokenEstimator` 使用统一 JSON 模板序列化 messages 和 tools，以字符规模乘校准系数得到请求前估算值。

```text
请求前：统一模板 → 临时估算 → 预算判断
请求后：模型真实 input_tokens → 更新校准系数
```

约束：

- 每次估算结果不持久化；
- 只在内存中保留当前模型的校准系数；
- 系数只向更保守的方向更新，避免历史样本导致低估；
- tools schema 在一次 Run 内生成一次，并被各 Round 复用；
- 真实 token 消费只记录模型返回的 input/output usage，不记录估算值。

### 统一模型调用入口

Planner 和主 Agent Round 都通过同一个 `ModelCallService`：

```text
ModelContext + tools
        ↓
ModelCallService
        ↓
ContextBudget.assess()
        ├─ exceeded → ModelCallBlocked
        └─ allowed  → LLMClient.chat()
                         ↓
                    LLMCallResult
                    ├─ LLMResponse
                    └─ LLMUsage
```

项目预算超限返回：

```text
status = blocked
reason = context_budget_exceeded
```

模型最终拒绝超长请求返回：

```text
status = blocked
reason = context_length_exceeded
```

两者状态相同，但原因不同，可以判断本地估算和模型配置是否可靠。

### Planner 与 Conversation

RunRuntime 接受用户输入后，先把 user message 写入 Conversation，再启动 PlanningMode。

Planner 不再只接收孤立的 `user_message`，也不再通过 `build_plan_messages()` 自建第二套消息路径，而是：

```text
完整 Conversation
    ↓ ContextManager 投影并注入 Planner 指令
Planner ModelContext
    ↓ ModelCallService，tools=[]
PlanCallResult
├─ Plan
└─ LLMUsage
```

Planner 能读取完整历史，但 Planner prompt、原始 JSON 和 Plan 不写回 Conversation。Conversation 仍然只表示用户与主 Agent 的可恢复协议轨迹。

### 真实 usage 记录

每次成功返回 `LLMCallResult` 的调用都会生成 `llm_usage` Checkpoint，并自动进入 Run Trace：

```text
kind: llm
reason: llm_usage
data:
  stage: planning | agent_round
  round_index: int | null
  input_tokens: 模型真实值
  output_tokens: 模型真实值
```

usage 不进入 Conversation，也不复制到 SessionRunRecord。Run 的总消费可以从 Run Trace 中的 usage checkpoints 派生。

### 最终流程

```text
SessionRuntime
    ↓
RunRuntime
    ├─ 将当前 user message 写入 Conversation
    ├─ Planner：完整 Conversation 投影 → 预算 → 模型调用
    └─ Agent Round：重新投影 → 预算 → 模型调用 → 工具循环

模型成功响应
    ├─ 使用真实 input_tokens 校准估算系数
    └─ 将真实 input/output tokens 写入 Run Trace
```

### 本阶段明确不做

- Safe Compression 或消息截断；
- Memory、Retrieval、Workspace 上下文；
- 统一超参数配置入口；
- Planner 的独立 retry 策略；
- 将估算值写入消费记录。

当前控制台暂时显式使用 `MODEL_CONTEXT_LIMIT = 32_768`，后续再接入统一超参数入口。

### 验证结果

全量测试 `110/110` 通过，覆盖：

- 75% 项目预算判断；
- messages 与 tools 统一估算；
- 校准系数只保存最新保守值；
- 预算超限不调用模型并返回 blocked；
- Planner 使用完整 Conversation 且 tools 为空；
- Plan 不污染 Conversation；
- Planner 与主 Agent 共用 ModelCallService；
- Planning 和 Agent Round 分别记录模型真实 usage；
- 项目预算超限与模型硬限制原因分离；
- 同一 Round 的 retry 继续复用同一个 ModelContext 快照。

这一步最重要的认知是：

> Conversation 保存完整事实，ModelContext 表示当前消息投影，ModelCallRequest 表示完整模型输入，ContextBudget 只判断当前请求是否允许发送。
