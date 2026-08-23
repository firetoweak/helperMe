# Phase 6A · Goal Loop

## 目标

Goal 表达最终目标、冻结的完成标准、跨 Turn 生命周期和追加式 Judgment 历史。

```text
用户设置 Goal
    ↓
自动推导 Completion Contract v1
    ↓ 冻结
执行一次完整 Executor Agent Turn
    ↓
独立 Judge Turn 主动验证
    ├─ done     → Goal completed
    ├─ continue → 注入 Judge feedback，自动开启下一 Turn
    └─ pause    → Goal paused，等待用户修订或恢复
    ↓
直到完成、暂停或耗尽 max_goal_turns
```

## 职责边界

| 概念 | 唯一职责 |
| --- | --- |
| Goal | 最终目标、Contract、Turn/Judgment 历史和生命周期 |
| Executor Turn | 根据 Goal、Contract 和 Judge feedback 自主行动 |
| TodoList | 单个 Executor Turn 内的柔性认知工具 |
| Judge | 独立检查真实状态并决定 done / continue / pause |
| GoalApplicationService | 编排 Contract → Executor → Judge → continuation |

显式 Plan 是 Executor 的可选能力；TodoList 是单个 Turn 内的柔性认知工具。

## Completion Contract

Contract 由独立 Contract Compilation Turn 根据用户 Goal 自动推导。每条标准都包含语义描述、权限来源和具体证据要求：

```text
CompletionCriterion
├─ id
├─ description
├─ authority: user | inferred
└─ evidence_requirements
```

权限规则：

- 每个 Contract 至少保留一条 `user` 标准；
- Executor 只能读取当前冻结版本，不能提交修改；
- Judge 可在 Turn 边界修订 `inferred` 标准和验证方式；
- Judge 不能新增、删除或改写 `user` 标准；
- Contract Revision 只从下一 Executor Turn 生效，不能和 `done` 同时提交；
- 修订采用追加式版本历史，不覆盖旧版本。

冻结粒度是“一次 Executor Turn”，不是整个 Goal 永久不可修改。这既防止 Executor 移动球门，又允许系统修正首次自动推导中的错误。

## 独立 Judge 与实际验证

Judge 使用独立 Session，不继承 Executor Conversation。Composition 为每个 Session 创建私有 TurnRuntime，因此 Judge 与 Executor 的 Conversation、ContextState、TurnEvidence 和临时工具注册表相互隔离；二者可以安全复用同一个模型与无状态 ModelCallService。

Judge 的基础工具白名单只包含读取、检索、`get_changes` 和 `execute_command`，不暴露 `write_file / apply_patch / replace_all`，因此不能直接修复业务文件。验证命令自身仍可能产生缓存或构建产物，Contract 应通过 workspace 要求核验最终状态；若未来需要强隔离，再引入验证副本，不在本阶段伪造“命令绝对只读”。

Judge Capability 允许读取真实工作区并执行验证，但提示词明确禁止修复。它必须调用 `submit_goal_judgment` 提交结构化结论。`done` 同时受到两层约束：

1. Judge 对 Objective、Contract 和真实状态进行语义判断，并列出证据；
2. CompletionGate 直接读取 Judge Turn 的 `TurnEvidence`，机械核验命令、退出码、超时和最终 workspace 状态。

Executor 的总结不是完成事实。缺少结构化证据时，TurnRuntime 的 completion barrier 会拒绝 `done`，Judge 可以补做验证或改为 `continue`。

## 状态机

```text
active   --start_turn--> active
active   --executor completed--> judging
judging  --done--> completed
judging  --continue--> active | exhausted
judging  --pause--> paused
active/judging --interrupt--> paused
paused   --resume--> 中断前可恢复状态
```

`max_steps` 限制单个 Turn 内的模型—工具循环；`max_goal_turns` 只统计已经到达 Judge 边界的完整 Executor Turn。中断的 Executor Turn 由 SessionTurnRecord 保存，不消耗 Goal Turn 配额。两者由 `model_config.yaml` 分别配置。

## 当前实现

- `plugins/goal/goal.py`：Goal、CompletionContract、ContractRevision、GoalTurn、GoalJudgment 与状态机。
- `plugins/goal/application.py`：同步驱动 Contract、Executor、Judge 和 continuation，并管理隔离 Session。
- `plugins/goal/capabilities.py`：ContractCompilation、GoalExecutor、GoalJudge 三种临时 Capability。
- `plugins/goal/submissions.py`：Contract 与 Judgment 的 Turn 内提交缓冲区。
- `plugins/goal/verification.py`：GoalVerification 与只解释机器事实的 CompletionGate。
- `plugins/goal/store.py`：进程内 GoalStore；一个 Session 同时只允许一个未结束 Goal。
- `plugins/goal/console.py`：`/goal <objective>` 入口和活动 Goal 的后续路由。
- `core/turn_host.py`：插件只依赖通用 Session/Turn 公共端口，Core 不含 Goal 词汇。
- `core/tools_runtime/turn_invocation.py`：Turn 级 Capability 与 RuntimeMode 临时覆盖。

普通 Turn 不携带 Goal Capability，也看不到 Goal 工具。删除 Goal Plugin 后 Core 无需修改即可独立运行。

## Plugin 边界回看

Goal 是第一个 Plugin，当时主要通过“Core 不导入 Goal、Goal 只消费 TurnHost 与 TurnInvocation”确认代码依赖方向。到 6B 设计第二个 Plugin——MCP 外部能力支架时，Plugin 的完整语义才进一步清晰：Plugin 不是一种具体工具，也不是 Core 的分层目录，而是建立在 Core 公共端口之上的可选 Agent 辅助支架。

Goal 属于工作流型 Plugin：拥有 Goal、Contract、Judge 和跨 Turn 状态，通过公共 Turn 端口组织 Core 能力。删除 Goal 后，普通 Agent 仍可运行，只失去目标循环能力。Goal 的领域对象、存储和控制台入口均保留在 `plugins/goal`，因此当前实现符合这套更明确的边界。

这次回看不要求重构 Goal。它反而验证了一个可复用判断：若未来 Plugin 暴露出 Core 公共端口不足，只补充与该 Plugin 领域无关的通用语义；不得把 Goal、MCP 或其他具体能力的生命周期写进 Core。

## 当前验证

新测试覆盖：

- user 标准不可由 Judge 降低；
- inferred 标准只能在 Turn 边界版本化修订；
- Contract v2 只影响下一 Executor Turn；
- Executor 与 Judge 使用隔离 Session；
- Judge Session 使用私有 TurnRuntime，不继承 Executor 上下文；
- `done` 必须引用证据并通过真实命令与 workspace 门禁；
- `continue` 自动注入 feedback；
- pause / resume / max_goal_turns exhausted 状态迁移。

全量自动化回归通过。真实模型 Goal Loop benchmark 也已完成：`qwen27b` 在一个 Goal Turn 内自动编译 5 条 Completion Contract 标准，Executor 完成目标，隔离 Judge 以非空证据判定 `done`，最终状态为 `completed`，且工作区前后状态一致。可复现脚本为 `tests/benchmarks/phase6a_goal_live_benchmark.py`。

## 下一步边界

6B 继续研究 Turn 期 Skill / Toolset Progressive Loading。Goal 只消费通用 TurnInvocation，不负责决定具体能力如何发现和加载。

持久化、后台调度和多 Agent Judge 都后置；当前不为了未来可能性扩张 Goal 聚合。
