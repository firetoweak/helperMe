# 判定标准与 Judge（延后决策）

> 状态：产品规则仍以此文为准；实现见 [判定标准与 Judge 实现总结](判定标准与Judge实现总结.md)（2026-08-22）。  
> 决策日期：2026-08-22  
> 触发条件：眼下路线完成后实现——新栈 Model Context 投影、token 预算、Artifact 外置、最近保护窗已能支撑一句 `UserMessage` 之后的长 Step 链。  
> 校准：[项目架构方向](../项目架构方向.md)、[Agent Runtime 状态推进模型](Agent%20Runtime状态推进模型.md)

本文冻结产品策略。Runtime 内核仍不增加 Goal 新建模；分类、inferred 版本和 Judge 判定作为 Journal 事实，由 Host 编排。

## 为什么当时不写代码

Turn 在新架构里只是人类交互投影，不是执行量子。旧 Goal Loop（Executor Turn → Judge Turn → 下一 Turn，外加 `max_goal_turns`）因此不再是必需编排。

但「目标是否满足」仍可能需要语义判断。架构模型已要求：Evaluator 不得在隐藏路径调用 LLM；判定必须由普通 Step 或显式 Judge Step 留下决策事实。眼下路线完成前，长任务会先死在上下文被切掉，而不是死在没有 Judge。这条已经不再挡住实现。

## 已钉死的产品规则

> **人负责目标；模型补全判定；人随时可以放松推断标准；只有人明确换事时才改目标。**

拆开后是：

| 层 | 权威 | 用户原话不完整时 |
|---|---|---|
| `user` 标准 | 当前任务的用户原话 | 允许不完整，不要求人把终止条件说死 |
| `inferred` 标准 | 独立分类/编译得到，如「必须跑测试」 | 系统补全；干活模型不能改 |
| 后一句用户话 | 默认改 `inferred`（降级或推迟） | 「先改完，测试一会再说」不换任务 |
| 换目标 | 仅当人明确改做别的事 | 拿不准则 `WAITING`，问一句 |

`inferred` 被放松时不要删除，只做版本化推迟。删掉之后，人回头说「改完了」时 Judge 会假装从来不必测。

## 不要把旧 Goal 整包搬回来

旧 Goal 捆了五样东西。Turn 消失后，只有编排那一段该死：

```text
Executor Turn 结束 → 自动 Judge Turn → continue 再开下一 Turn
```

仍需要、且现已实现的是：

- 当前任务的 `user` 原话（球门）
- 冻结的 `inferred` 判定标准（版本 + 谁推断的，必须是 Journal 事实）
- 隔离的 Judge（另一套提示词、只读工具、不继承干活对话）
- **continue 特权**：没有新 `UserMessage` 也可以再开 Step

新架构不恢复「Goal 模式 vs Plain 模式」并列。默认任务仍然是：干活 → `deliver` → `WAITING(user_message)`，人就是 Judge。

## continue 特权（第三条，已收敛）

不是任何任务都可以在没新用户话时继续跑。仅当这次工作需要严格收口时，Judge 说 `continue` 才允许 Stream 保持 `RUNNABLE`。

分类（要不要严格收口）和标准（核对什么）分开：

```text
分类    这次能不能没新用户话也继续、能不能 COMPLETED
标准    继续/完成时核对什么
```

分类输入应是确定性事实（本 Stream 是否写了文件、是否碰了权限、是否跑过命令），不要只靠干活模型的 prompt 场景表。改文件可以当分类输入，不能当口头规则：执行模型有动机把任务说成「不用测」。改权限是 Approval / Policy，模型不能自行放宽。

干活模型只能读当前冻结标准，不能投票否决分类结果。分类结果一旦写入 Journal，就必须按该版本执行，直到人改 `inferred` 或明确换事。

## 明确不在本文范围内

- 不在 Runtime Core 增加 Goal 聚合、Goal Loop 或 `TurnHost` 续跑。
- 新架构不引入 TodoList（见下节）。
- 不把独立 Judge 做成旧 Core 的 Session / Compilation Turn / CompletionGate。
- 不把 Level 2 摘要当完成证据。

## 新架构不用 TodoList

这不是「判定做完后价值下降、先观察再删」。新架构里它没有位置。

旧 Todo 同时想当战术草稿、完成屏障和任务状态，三件都做不好：真实任务里很少被用；Sync Barrier 是同步负担；勾选是自述，不是证据。Journal 世界里这三件已有主人：

```text
刚做过什么     Step / Outcome（保护窗内留原文）
做成是什么     user / inferred 判定标准
能不能停       人（默认 WAITING）或独立 Judge + 证据
```

再留一份模型可改写的清单，就是第二事实源。战术便条也不迁：近况已经在最近 Step 里，不必再维护一份会漂的平行列表。

因此：`agent_runtime` 与新栈适配层不挂 `rewrite_todos`、不做 Todo 模式路由、不上 Todo Sync Barrier。旧 `core/` 里的 Todo 冻结保留，供对照入口使用，不作为新设计的待选项。

## 眼下路线（必须先完成）

```text
Journal 可见事实
  → 消息投影
  → token 预算
  → 超大工具/命令结果外置为 Artifact 引用
  → 上一句 UserMessage 之后的 Step/Outcome 不脱水
  → 窗外且已消费且成功的结果才 stub
  → 摘要只做投影缓存，不写回 Journal 当事实
```

该路线完成后，按本文实现分类事实、inferred 版本和可选 Judge。实现位置与验收见 [判定标准与 Judge 实现总结](判定标准与Judge实现总结.md)。
