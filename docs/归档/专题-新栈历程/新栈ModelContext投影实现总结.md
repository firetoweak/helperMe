# 新栈 Model Context 投影实现总结

> 状态：完成（2026-08-22）  
> 位置：产品 / adapter，不进入 Runtime 内核  
> 对应路线：[前六章回补方向](前六章回补方向.md) 眼下切片

## 做了什么

Journal 仍然只保存可见执行事实。模型输入由 adapter 投影：

```text
冻结可见 Event
  → 协议消息（project_chat_messages，原文翻译）
  → 超大 tool body 外置为 Artifact
  → 保护窗：上一句 UserMessage 之后必留原文
              再从尾部补足 recent_protection_tokens
  → 窗外、批次完整、已消费、成功 → Level 1 stub
  → ContextBudget 评估；超预算 fail fast
```

摘要（Level 2）不做。`command_id → artifact_id` 是投影缓存，不写回 Journal。

## 保护窗

硬条件：上一句真实 `UserMessage`（不含 interrupt）之后的 Step/Outcome 不做年龄脱水。

软条件：从尾部倒推，直到估算 token 达到 `recent_protection_tokens`（默认 10_000），避免上一句用户话很短时把更近的历史切掉。

失败、未消费、不完整批次保持原文。Interrupt 不当成新的用户意图锚点。

## Artifact

- 抽屉按 Stream 隔离（`FileArtifactGateway`，根目录 `~/.helperme/runtime_streams`）。
- 单条结果超过 16_000 字符：执行时写入 Store，Journal 只留 stub + `artifact_id`（避免 128KB freeze 上限，也避免打满保护窗）。
- 保护窗内的超大结果可以体积外置，这不是年龄脱水。
- 模型通过 `read_artifact` 分页回读；Binding 使用 `AttemptContext.stream_id`，不能跨 Stream 猜 id。

## 预算

使用 adapter 自己的 `InputBudget` + `TiktokenEstimator`，读取宿主配置的 `model_context_limit` / `input_budget_ratio`。超预算抛 `ModelContextBudgetExceeded`，不静默截断，也不生成摘要冒充事实。不再调用 `core.context`。

## 明确没做

- Runtime 内核、Goal、Judge、TodoList
- Level 2 摘要写回或当完成证据
- Turn 产品映射、Approval
- 把 Artifact 引用写进 Event.artifact_refs（本切片投影缓存已够用；需要跨进程重放同一 stub 时再补）
- 新栈文件工具 / LLM 客户端仍经 `adapters/legacy.py` 借用冻结 Core；投影、预算、Artifact 已从 Core 切开
