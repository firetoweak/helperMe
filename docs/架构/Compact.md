# Compact：后台交接与上下文窗口

2026-09-09。当前代码采用同一业务 Session 的 ContextWindow 切换；完整决策见[Self-Handoff 实施设计](Self-Handoff实施设计.md)。真实模型的摘要质量、实际缓存命中率与预算阈值仍待评测。

## 执行模型

```text
B 在安全位置 P 冻结当前模型输入
  ├─ B 继续业务，新增事实进入同一 Journal
  └─ 独立 H Worker：原 SYS / TOOLS / 消息 + 交接要求
       → 可多轮回读冻结来源
       → 非空文本且无工具调用 → 接纳 handoff

B 到安全位置 Q
  → compact.handoff_created
  → compact.window_rolled_over
  → 下一次决策读取新窗口
```

B 的 Session identity、工具激活、委派和决策游标持续存在。H 有独立 Journal 和执行预算，不竞争 B 的 Step，也不继承其待执行 Command。H 使用相同模型工具定义，但执行侧仅允许 `read_compact_source` 回查 P 以内的冻结消息及其工具 Artifact。其他工具调用明确失败；不修改工作区、不发送用户回复。

最终交接文本转为内部 `_accept_handoff` Command，在提交后通过 Host 接纳；该内部 Binding 不向模型暴露，不改变冻结工具列表。WAITING 不是成功判据，只有没有工具调用的非空文本才触发交接接纳。

## 窗口和来源

窗口发布位于直接工具及等待授权收口后的决策边界，不要求普通 SubAgent 全部回收。新窗口保留 handoff、当前能力目录、P 前最后一个完整助手轮次、全部 `(P,Q]` 原样新增消息及之后的新事实。最后一个助手轮次连同其工具结果一起保留；不将巨大的旧用户输入作为固定尾部重复带入。

窗口事实包含 id、parent、upto、cutover、recent_tail_start、context 与 bundle 引用。Assistant 从 Journal 重建 active window；Runtime 始终归约完整 Journal，窗口隐藏的旧事件不被删除，也不会重新执行。

来源材料保存实际冻结请求、带事件序号的模型消息、原始消息投影与允许读取的 Artifact。多次压缩的来源窗口和请求引用说明实际阅读过哪些材料。历史回读用事件序号，不把它冒充 Step 编号。handoff 保存原有证据强度，遇到疑点才回查，不做统一接班后验。

## 预算与恢复

Prompt 要求仅补关键缺口、禁止重复回读和扩展调查，剩余最后一次调用时追加收尾提醒。Harness 使用以下配置：

- `compact_max_calls`：默认 8 次；Host 在模型调用前持久计数，重启不重置。
- `compact_timeout_seconds`：默认 300 秒；任务创建时保存绝对截止时间，Worker 超时中断生成和等待。
- 输入预算沿用 `model_context_limit` / `input_budget_ratio`，留出输出空间；每次模型调用和窗口发布前检查。

`compact_threshold_ratio` 默认 0.55，相对于输入预算触发后台准备。失败保留原始诊断，不自动重试或发布半成品；B 容量足够时继续，容量不足时暂停模型调用但继续接收输入。

Host 的 `conversations.sqlite` 仅保存后台任务、计数、结果和发布准备，不再保存业务会话路由。开发阶段只维护当前表结构，不读写或检查数据库版本号；首次启动自动建库。旧结构的开发数据库需在停止相关任务后一次性备份重建，不提供自动迁移或兼容路径。

先保存交接 Artifact 和发布准备，再向 B 幂等追加交接与窗口事实，最后标记任务已发布。进程中断后继续同一发布，不重新生成已接纳 handoff；过期来源窗口不能覆盖当前窗口。

旧版本数据库与旧 compact 种子不兼容，不自动迁移或删除用户数据。普通 Session / SubAgent 机制不变，Runtime Core 未增加 compact 语义。

## 实现与验证

`compact.py`：来源读取、后台请求组装、窗口投影与 Worker 决策边界。
`compact_host.py`：后台任务启动、交接接纳和同 Session 发布。
`compact_store.py`：任务持久化、预算与发布准备。
`decision.py`：冻结工具面、只读调用限制、最终文本转内部提交。
`worker.py`：总耗时中断与窗口发布入口。

测试覆盖后台并行、尾部与输入幂等、同 Session 发布、重启恢复、多轮回读、循环预算、总超时、写调用拒绝、来源上界和多窗口重建。业务工具结果脱水本轮未调整；H 冻结前缀不随回读轮数重新脱水。
