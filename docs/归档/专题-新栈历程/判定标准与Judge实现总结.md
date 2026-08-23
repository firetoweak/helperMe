# 判定标准与 Judge 实现总结

> 状态：完成（2026-08-22）  
> 位置：Journal 增加两类产品事实；编排在 Host / adapter，不进入 Runtime Goal 建模  
> 产品规则：[判定标准与 Judge 延后决策](判定标准与Judge延后决策.md)

## 做了什么

旧 Goal Loop（Executor Turn → 自动 Judge Turn → continue 再开下一 Turn）没有搬回来。新栈仍然默认：干活 → `deliver` → `WAITING(user_message)`，人就是 Judge。

严格收口只在分类事实说「这次可以没新用户话也继续 / 可以 COMPLETED」时才启用：

```text
Journal 事实
  CriteriaCommitted   user 原话 + 冻结 inferred（版本化，推迟不删除）
  JudgmentCommitted   独立 Judge 的 done / continue / pause

Host
  分类来自确定性工具事实（写文件 / 跑命令），不是干活模型投票
  后一句用户话默认放松 inferred；明确换事才改 user 目标
  干活模型只读当前冻结标准
  Judge 另一套提示词、只读工具、不继承干活对话
  continue 是决策触发，没有新 UserMessage 也可以再开 Step
```

Runtime 内核只认识这两类 Event：`CriteriaCommitted` 不触发决策；`JudgmentCommitted(continue)` 触发决策。没有 Goal 聚合根，没有 `max_goal_turns`。

## 分类与标准

| 层 | 谁写 | 何时 |
|---|---|---|
| `user` 目标 | 当前任务的用户原话 | 首句；或人明确换事 |
| `strict_completion` | 本 Stream 是否成功写过文件或跑过命令 | 事实变化时升版 |
| `inferred` | 模板编译（工作区核对 / 行为验证） | 随事实追加；人放松时 `deferred` |

干活模型不能否决分类。`inf-verify` 被推迟后仍留在 Journal 里，Judge 不再把它当当前必须满足的条件。

## continue 特权

仅当 `strict_completion=true` 且最新 Step 声明 `COMPLETE` 时，Host 才跑独立 Judge：

- `done` → 允许 Finalization 写成 `RuntimeCompleted`
- `continue` → 写入判定事实，Stream 保持 `RUNNABLE`，干活模型再开 Step
- `pause` → 不 Finalize，回到 `WAITING(user_message)`

聊天任务没有写文件、没有跑命令时，模型自己的 `COMPLETE` 仍直接收口，不经过 Judge。

## 文件

- Runtime：`agent_runtime/events.py`、`codec.py`、`state.py`、`runtime.py`（`record_fact` / `snapshot`）
- Host：`adapters/criteria.py`、`adapters/judgment.py`、`adapters/runtime_host.py`
- 默认入口：`python console_chat.py`

## 还没做

- 换目标拿不准时主动 `WAITING` 问一句（当前启发式认不出就当同一任务继续）
- inferred 的自然语言编译（现在是确定性模板）
- Approval / 权限碰触作为分类输入
- 把 `artifact_refs` 写进 Event；Level 2 摘要仍不做完成证据
