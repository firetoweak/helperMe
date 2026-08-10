## Phase 2 · TodoList 总结

TodoList 是面向单个 Run 执行的可变任务认知状态，不是真正的 Plan，也不是任务调度系统。
Executor 将其作为当前行动参考，而不是不可违背的指令序列。
真正的 Plan 保留给未来针对大目标进行 Todo 拆分、依赖组织与 SubAgent 委派的规划层。

目标：让 agent 在执行长任务前形成可审阅的 TodoList，并在执行过程中通过 `rewrite_todos` 自主维护。
TodoList 主要服务模型执行，不追求持久化、跨 Run 恢复或复杂调度。

### Benchmark

每个 Run 在执行前根据完整 Conversation 选择 `plain/todo`；
简单任务跳过 Todo 初始化，复杂或不确定任务进入 TodoMode；
面对一个需要读文件、分析、修改、验证的任务，Todo 初始化阶段只开放 `rewrite_todos` 并创建步骤清单；
Executor 能把 TodoList 作为柔性行动参考；
工具产生新观察后，Executor 可继续行动，也可随时 `rewrite_todos`；
最终回答前必须通过 Todo Sync Barrier。

### 设计

- `RuntimeModeRouter` 在当前 user message 写入 Conversation 后、创建 mode state 前运行；路由按 Run 生效，不固定整个 Session。
- Router 不开放工具，只返回严格的 `mode/reason` JSON；非法响应被严格拒绝，但动态路由链路在同一 Run 降级到 `plain`。
- 路由结果只进入 checkpoint / usage，不写回 Conversation；`RunRuntime` 仍统一管理模型调用和上下文准备。
- `plain` 直接进入 Agent Round；`todo` 先执行受限 Todo 初始化。讨论、评价、解释或提出方向属于 `plain`；只有明确授权执行后才继续判断是否需要 `todo`。
- Todo 初始化是同一模型的只读阶段，只开放 `rewrite_todos`，首次快照包含 2 到 6 个可审阅的长任务步骤。
- 删除独立 Planner / Replanner；同一个模型在后续轮次兼任 Executor 与 Todo 审查者。
- `rewrite_todos` 是初始化和后续修改的唯一入口；完整快照支持状态修改、新增、删除、调序、拆分、合并与取消。
- `TodoPhase` 管理 `UNINITIALIZED / ACTIVE / COMPLETED` 生命周期。
- `TodoSyncState` 独立管理 `CLEAN / DIRTY` 同步状态。
- 外部工具批次后进入 `ACTIVE + DIRTY`；执行过程不强制立即同步。
- 最终回答前必须同步，并且所有必要 Todo 都是 `done/cancelled`。
- `COMPLETED + DIRTY` 为非法组合。
- TodoList 是 Run 局部状态；TodoMode 是不持有运行状态的生命周期策略。
- TodoList 与 revision 进入 checkpoint / run trace，不把初始化模型原始响应写回 Conversation。

### 后续收尾

- 只读/禁止修改不是 TodoList 的状态职责。后续 Runtime Mode Router 已把讨论与明确授权执行分开；工具权限与能力暴露属于应用边界和 Phase 6B，不向 TodoList 塞入一套权限系统。
- 真实长任务稳定性已在 Phase 5.7 Agent Benchmark 中回补：TodoMode 完成任务初始化、执行中同步、失败测试定位、修改、重测、构建和最终同步；一次非法 rewrite 被边界拒绝后可按契约修正。

因此 Phase 2 按“Run 内柔性行动参考”的原目标完成。跨 Run 目标、任务依赖和委派继续由 Phase 6 承担。

### Phase 6A 压力测试补充（2026.08.10）

真实 Goal 闭环中出现过 `rewrite_todos` 参数被模型错误序列化、Todo 最终同步失败，但 Task 的真实命令和工作区验收仍然成立。这进一步确认了本章的职责边界：

- TodoList 是 Run 内的柔性行动参考，不是 Task 状态、验收事实或权限系统。
- Todo Sync Barrier 只保证 Todo 快照内部一致，不能替代测试、命令退出码和 `get_changes`。
- Todo 工具的外部输入仍按 schema 拒绝；内部不增加猜测、字符串反序列化或补救分支。

因此本次没有把 Todo 提升为 Goal 的完成条件，也没有为模型偶发格式错误放宽工具契约。
