## Phase 5.3 Safe Compression 总结

这一阶段完成了同一 Session 内的安全上下文压缩闭环：Conversation 继续保存完整事实轨迹，运行时只更新 ContextState，并由 ContextManager 生成可发送给模型的 ModelContext 投影。压缩不会修改原始消息，也不会更换 session_id。

> **L1 语义修订（后继）**：Level 1 已从高低水位「整批墓碑折叠」改为**持续性工具脱水投影**。`micro_compacted_through_message_id` 由 `tool_artifacts: message_id → artifact_id` 取代；投影保留 tool call 外壳，仅替换已消费成功且位于 recent 窗外的 tool body，首次脱水懒写入 ArtifactStore。下文若仍写「墓碑 / 高低水位」，以本修订为准。

### 问题边界

Safe Compression 解决的是合法、有界消息在长期 Session 中不断累积的问题，不负责补救单条无界输入。

进入本阶段前已经建立两个前提：

- 用户输入在外部输入边界限制；
- 单次工具结果在 Tool Result Budget 边界限制，超大完整结果进入 Runtime Artifact。

因此压缩只处理历史累积，不在投影阶段偷偷截断当前用户输入或超大工具结果。

### Conversation 与 ContextState

Conversation 的职责进一步收敛为追加式事实日志：

```text
ConversationMessage
├─ message_id    领域记录标识
└─ payload       OpenAI 协议消息
```

`message_id` 不进入模型协议，只用于稳定引用压缩边界。Conversation 中的原始内容、顺序和工具链不会因压缩改变。

Session 维护一份最小派生状态：

```text
ContextState
├─ summary
├─ summarized_through_message_id
└─ tool_artifacts
```

其中：

- `summary + summarized_through_message_id` 表示 Level 2 已提交的历史摘要及事实边界；
- `tool_artifacts` 表示 Level 1 已为哪些 tool 消息建立可回读 artifact 映射；
- ContextState 不保存 ModelContext 副本，不复制 Conversation 消息。

每次模型调用仍生成新的不可变 ModelContext 快照。同一 Round 的 retry 复用同一个快照，不在 retry 之间重新压缩。

### 统一上下文准备入口

Planner 和 Agent Round 不再各自直接调用 ContextManager，而是统一经过 `ContextPreparationService`：

```text
Conversation + ContextState + runtime instructions + tools
        ↓
ContextPreparationService
        ├─ Level 1 决策
        ├─ 必要时 Level 2 摘要
        ├─ ContextManager 投影
        └─ ContextBudget 重新评估
        ↓
PreparedContext
├─ ModelContext
├─ candidate ContextState
└─ 压缩决策与预算结果
```

ContextPreparationService 只生成候选状态，不直接修改 Session。RunRuntime 在一次 Run 内维护当前候选状态，通过 RunResult 返回；SessionRuntime 在 Run 结束时统一回写。这样 Session 后续 Run 会继续复用已经提交的摘要和压缩边界。

### Level 1：持续性工具脱水投影

Level 1 在每次 ContextPreparation 执行：对 recent 保护窗外、已消费且成功的历史 tool body 做确定性脱水，不依赖高低水位。

只有完整工具批次同时满足以下条件，其中的 tool 消息才会进入脱水集合：

- tool call 与全部 tool result 完整对应；
- 所有结果都成功；
- 后续已经出现 Assistant 响应，说明该批结果已被模型消费；
- 整个批次位于 recent 保护窗之前。

投影保留 assistant `tool_calls` 与 tool 消息外壳，仅替换 tool `content` 为可回读 stub（`artifact_id` + hint）。首次脱水时写入 ArtifactStore，并记入 `ContextState.tool_artifacts`；已外置结果复用已有 `artifact_id`，不重复落盘。

失败结果、未消费结果、不完整批次、近期保护窗口内消息和普通文本保持原样。映射只增不改同一 `message_id`；Conversation 全文不变。

### Level 2：增量 Auto-Compact

Level 2 是最后兜底，只在 Level 1 脱水投影后仍超过项目输入预算时触发。没有超过输入预算时，不会调用摘要模型。

第一版使用普通 prompt 控制 LLM 输出自由文本摘要，不定义结构化摘要模型。摘要必须保留用户目标与约束、完成和待完成状态、关键工具事实及必要标识。

Level 2 的安全边界固定为：

> 只摘要当前 Run 开始前的历史。

RunRuntime 在添加当前 user message 前记录上一条消息的 message_id，作为本 Run 的临时 Level 2 上界。当前 Run 的用户目标、工具步骤和模型响应保持原文，不进入摘要。

摘要输入不是重新读取完整原始工具结果，而是使用 Level 1 处理后的旧历史投影：

```text
首次：旧历史的 Level 1 投影 → S1

后续：S1 + 新增旧历史的 Level 1 投影 → S2
```

已经被旧摘要覆盖的 Conversation 前缀不会再次全部送给摘要模型。摘要成功后，ModelContext 在 system prompt 后插入一条合成 Assistant 消息：

```text
工作交接摘要：
{summary}
```

摘要正文不写回 Conversation。

### 原子提交与失败语义

Level 2 先生成候选摘要和候选 ContextState，然后重新完成投影、工具协议校验和完整请求预算评估。

```text
Level 1 后仍超预算
        ↓
检查当前 Run 前是否存在可摘要历史
        ↓
生成候选摘要
        ↓
候选 ModelContext 重新评估
        ├─ allowed  → 提交新 ContextState → ModelCall
        └─ exceeded → 丢弃摘要候选 → blocked
```

提交新摘要时会丢弃已被摘要边界覆盖的 `tool_artifacts` 条目，因为对应 Level 1 历史已经不再出现在模型投影中。

以下情况不会提交候选摘要：

- 当前 Run 前没有新增可摘要历史；
- 摘要调用本身超过预算；
- 摘要模型调用失败或返回非法响应；
- 候选边界或工具协议不合法；
- 摘要后完整请求仍超过输入预算。

摘要模型已经产生的真实 usage 仍会记录，但不能作为提交无效 ContextState 的理由。摘要后仍超预算属于 blocked；摘要模型调用失败按模型调用失败处理。

### 可观测性与用户提示

Level 2 会产生上下文压缩 checkpoint，记录：

- 压缩级别；
- 摘要边界 message_id；
- 压缩前后估算 token；
- 输入预算；
- 候选是否被接受。

摘要正文不会复制到 checkpoint。摘要模型的真实 input/output usage 使用独立的 `context_summary` usage checkpoint 记录。

Level 2 成功并继续完成本轮任务后，最终回答前会增加一次简短提示：

```text
本轮已执行上下文压缩。
```

结构化 checkpoint 面向上层消费者，简短提示面向真实用户。

### 最终运行链路

```text
SessionRuntime
    ↓ 传入 Session.context_state
RunRuntime
    ├─ 记录当前 Run 前边界
    ├─ Planner ContextPreparation
    └─ 每轮 Agent ContextPreparation
            ├─ Level 1 持续性工具脱水（落盘/映射 + 投影）
            ├─ ContextBudget 评估
            ├─ 必要时 Level 2
            └─ 最终 ModelContext
    ↓
ModelCallService
    ↓
RunResult.context_state
    ↓
SessionRuntime 原子回写 Session.context_state
```

Conversation 始终保持完整，ContextState 在同一 Session 中持续复用，ModelContext 则是每次调用的临时执行快照。

### 当前明确不做

- 结构化摘要字段与语义等价校验；
- 当前 Run 内已闭合步骤的 Level 2 摘要；
- `protected_message_ids` 和稀疏原文保留；
- 普通 Assistant 文本的 Level 1 压缩；
- 自动识别长期约束和事实重要性；
- 新建 session_id 或把 Auto-Compact 当作 Handoff；
- 多模型摘要策略和独立摘要模型配置。

### 验证结果

全量测试通过，覆盖：

- Conversation message_id 与协议 payload 分离；
- ContextState 摘要投影和边界校验；
- 成功、失败、未消费和不完整工具批次的 Level 1 脱水行为；
- recent 保护窗、懒落盘 artifact 与幂等二次 propose；
- Planner 与 Agent Round 使用统一准备入口；
- ContextState 在 Run 内推进并由 Session 跨 Run 复用；
- Level 2 不读取当前 Run 的用户目标；
- Level 2 成功后裁剪已被摘要覆盖的 tool_artifacts；
- 摘要后仍超预算时拒绝候选状态并 blocked；
- compression checkpoint、summary usage 和最终用户提示；
- Conversation 原始内容和工具协议链不受压缩影响。

尚未完成的端到端 benchmark 是：同一 Session 连续触发两次 Level 2、验证 `S1 + delta → S2`，以及 interrupt/resume 后继续复用摘要状态。

这一阶段最重要的认知是：

> Conversation 是不可变事实源，ContextState 是 Session 可持续更新的最小派生状态，ModelContext 是单次调用投影。安全压缩不是删除历史，而是在明确边界、不变量和原子提交条件下更新模型看到的工作集。
