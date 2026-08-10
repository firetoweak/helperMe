
# Phase 6A · Goal / Task Management

## Benchmark

用户目标：检查项目中的两个问题，分别修复并完成测试。

Goal: 检查并修复两个问题

Task A: 定位两个问题
Task B: 修复问题一        depends_on: A
Task C: 修复问题二        depends_on: A
Task D: 整体验证          depends_on: B, C


然后回答这 5 个设计问题：
1. Goal 与 Session 是什么关系？一个 Session 能否讨论多个 Goal？
一个 Session 可以讨论多个 Goal，但每次只能存在一个 Goal。

2. 三个 Run 如何确认属于同一个 Goal？
调用边界显式传入 goal_id，不能靠对话内容猜测。

3. Task 状态的唯一真相放在哪里？
放在 Goal 聚合中。

4. 下一个 Task 怎么产生？
建议从 pending 且所有依赖均为 done 的 Task 中计算，不额外保存 next_task。

5. TodoList 如何避免成为第二份 Task 状态？

TodoList 只描述当前 Run 怎样执行某个 Task；跨 Run 完成状态仍只写入 Goal。

## 核心边界

- Goal 保存跨 Run 的任务计划、当前状态和追加式 Outcome 历史。
- Task 保存任务描述、验收标准、依赖关系和跨 Run 状态。
- Run 必须通过 `goal_id + task_id + run_id` 显式关联 Task，不根据对话内容猜测。
- TodoList 全部完成不等于 Task 完成；模型必须提交显式 TaskOutcome。
- Goal 只校验并应用 TaskOutcome / PlanRevision，不隐式生成关键关联或修改任务图。

## 核心状态机

```text
Task pending -> active
active + continue outcome  -> active
active + completed outcome -> completed
active + replan outcome    -> Goal.replan_required
apply PlanRevision         -> old Task.superseded + Goal.active
```

TaskOutcome 采用只追加历史，`(task_id, run_id)` 唯一；Task 只保留当前状态，历史执行事实不覆盖。

串行模式下，Goal 最多存在一个 active Task。`next_task()` 优先返回 active Task，否则按计划顺序返回第一个依赖均已完成的 pending Task。依赖表示“能否执行”，计划顺序表示“优先执行谁”。

重规划采用显式 PlanRevision。替代 Task 插入被替代 Task 的原计划位置，旧 Task 保留并标记为 superseded；依赖修改、未知引用、重复 ID、对 superseded Task 的依赖和依赖环均在应用前整体校验，失败时不产生部分修改。

## 当前实现

- `core/goals/goal.py`：Goal 聚合、Task、不可变 TaskRunLink、TaskOutcome、PlanRevision 与状态迁移。
- `core/goals/store.py`：内存 GoalStore；一个 Session 同时只允许一个未完成 Goal。
- `core/goals/commands.py`：每 Run GoalCommandBuffer；TaskOutcome 与 PlanRevision 只能提交到匹配的 Goal / Task / Run。
- `core/goals/capabilities.py`：Goal Task / PlanRevision 两种 Run Capability，以及绑定当前 Run 的模型工具。
- `core/goals/application.py`：连接 Goal 与 SessionRuntime，编排 Task 执行 Run 和重规划 Run。
- `core/goals/verification.py`：结构化 TaskVerification 与 CompletionGate。
- `core/tools_runtime/run_evidence.py`：保存未经裁剪的真实工具结果和 Run 起始工作区基线。
- `core/tools_runtime/run_invocation.py`：通用 RunInvocation / RunCapability 契约。
- `core/composition.py`：正式装配 GoalStore、CommandBufferRegistry 与 GoalApplicationService。
- `tests/core/test_goal_completion.py`、`tests/core/test_goal_full_loop_e2e.py` 等：门禁、异常恢复和完整闭环测试。

## Run 与 Task 结果的分离

SessionRunRecord 是 Run 运行状态的唯一真相；Goal 只保存不可变的 TaskRunLink，不复制 RunStatus。

```text
SessionRuntime 正常返回
    -> finish_task_run / finish_plan_revision_run
    -> 释放 Run 执行槽
    -> 若 completed 且 CommandBuffer 有命令，再应用 Outcome / Revision
```

`interrupted / blocked / failed` 只结束本次 Run，Task 保持 active，后续 Run 可继续同一 Task。为此回补 Session 状态机：blocked / failed 可以重新 start；interrupted 仍通过 resume。

RunRuntime 抛出内部异常时保留原始异常继续向外抛，同时先把 Session / Run 标记为 failed 并释放执行权；GoalApplicationService 再 abort 当前 Goal Run、关闭 CommandBuffer。Task 仍为 active，因此同一 Goal 可以由新 Run 恢复执行，不会永久卡在 open run。

## Run Capability 注入

GoalApplicationService 为每次 Task Run 或 PlanRevision Run 构造显式 RunInvocation。SessionRuntime 不解释 Capability，只把 Invocation 透传给 RunRuntime。

RunRuntime 按当前 Capability 构造临时 ToolRegistry，并合并三类内容：

- Capability runtime instructions：只进入本 Run 的上下文快照，不写入持久 Conversation system prompt；
- Capability ToolSpec：只存在于临时 Registry，Run 结束后自然释放；
- Capability completion barrier：模型未提交对应命令时拒绝最终回答。

Capability 同时声明是否允许基础工具。Task Run 可以读写 workspace、执行 Shell；PlanRevision Run 只暴露 `submit_plan_revision`，因此“规划不执行任务”由运行时工具边界保证，而不是依赖提示词自律。

`submit_task_outcome` 和 `submit_plan_revision` 的模型参数不包含 goal_id / task_id / run_id；关联信息由 ToolSpec handler 闭包绑定，模型不能选择或伪造。

PlanRevision 在工具边界先调用 Goal 的纯校验，非法 ID、依赖或环作为预期工具错误返回，允许模型在当前 Run 内修正；Run 正常完成后 Goal 再次校验并原子应用。

普通 Run 不携带 Invocation，不克隆 Registry，也看不到 Goal 工具。该通用入口已经形成 6B Progressive Loading 的前置基础。

## CompletionGate 与真实 RunEvidence

`TaskOutcome.evidence` 仍可作为给人的摘要，但不参与系统验收。Task 若声明自然语言 `acceptance_criteria`，必须同时声明结构化 `verification`：

- command requirement 核验当前 Run 中真实 `execute_command` 的命令、root/cwd、完成状态、超时状态和退出码；
- workspace requirement 核验真实 `get_changes`，并相对 Run 起始基线判断本 Task 新增的改动路径；
- CompletionGate 在 Run 最终回答前和 Application 落账前各检查一次。门禁拒绝时 Run 保持打开，模型可以继续执行验证后重新提交 Outcome。

工具结果写入 RunEvidence 发生在对话外部化之前，因此即使 stdout/stderr 因上下文预算被裁剪或存入 artifact，验收仍读取完整原始事实。

## 完整 Goal Benchmark（2026-08-10）

可重复脚本：`tests/benchmarks/phase6a_goal_three_run_benchmark.py`；真实模型：`qwen27b`。

实验使用隔离的 Python 项目制造两个问题：会员折扣实现错误，以及 README 指向的 `scripts/build.py` 不存在。所有 Run 共用同一个 Session 与 Goal：

| Run | Task | 模型结论 | 实际结果 |
| --- | --- | --- | --- |
| 1 | A：定位两个问题 | `completed` | 两条诊断命令均真实执行，工作区无改动 |
| 2 | B：修复折扣 | `completed` | 只修改 `calculator.py`，独立复测通过，构建问题保留 |
| 3 | C：修复构建 | `replan` | 识别到“只能编辑已存在脚本”与脚本不存在相冲突，没有越过边界新增文件 |
| 4 | PlanRevision | applied | 只修改任务图，工作区保持 Run 3 后的状态 |
| 5-6 | C1 / C2 | `completed` | 创建并验证 `scripts/build.py`，改动范围通过门禁 |
| 7 | D：整体验证 | `completed` | 单测、构建和最终改动范围全部通过 |

最终 Outcome 序列为 `completed -> completed -> replan -> completed -> completed -> completed`，原 C 被 supersede，Goal 最终为 completed。真实 `qwen27b` benchmark 的 19 项检查全部通过，包括独立测试、独立构建、PlanRevision 不执行任务、最终改动仅包含两个预期文件，以及 Git 历史未被模型修改。

全量回归：291 项通过，1 项跳过。至此 `completed` 已从“模型声称完成”提升为“系统依据当前 Run 的真实事实验收完成”。

## 对前置章节的反向校验

6A 是第一次把前五章能力放入“跨 Run、可重规划、必须系统验收”的连续任务中，因此反向暴露并回补了这些边界：

| 原章节 | 暴露问题 | 回补结果 |
| --- | --- | --- |
| Phase 1 | RunResult 只有运行报告，没有独立机器事实；临时工具只有生命周期隔离 | 增加 RunEvidence、RunInvocation 与 Capability 基础工具权限 |
| Phase 2 | Todo 容易被误当成 Task 完成状态 | 明确 Todo 只作 Run 内参考，格式失败不影响真实验收，也不放宽 schema |
| Phase 3 | Runtime 异常可能遗留 running Session 和 open Goal Run | failed 落账、释放控制权、abort/close 后原样抛出，允许新 Run 恢复 Task |
| Phase 4 | Goal 只能通过私有依赖手工组装 | GoalApplicationService 正式进入 Composition Root 和 AgentApplication |
| Phase 5.2.1 | 对话外置结果不能稳定承担系统验收 | 原始结果在 Externalizer 前进入独立 RunEvidence |
| Phase 5.6 | `get_changes` 的目录级 untracked 状态不够精确 | 返回全部未跟踪文件，并由 6A 建立 Run 起始基线 |
| Phase 5.7 | 命令能执行，但 Runtime 不能证明 Task 验收已满足 | CompletionGate 核验真实命令、完成状态、超时和退出码 |

沉淀出的通用原则：对话投影与执行事实分离；Capability 同时控制工具注入和权限收缩；所有跨 Run 状态必须设计异常退出路径；提示词负责引导，运行时契约负责约束。

## 下一步边界

6A 的执行闭环已完成。GoalStore 当前仍是进程内实现；持久化应在确有跨进程恢复需求时单独引入，不阻塞 6B Progressive Loading。

