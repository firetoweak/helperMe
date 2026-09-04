# SubAgent

> SubAgent 是一次性的异步函数式 Session：父 Agent 传入任务，子 Agent 独立执行并通过 `report` 返回结果，之后不能继续交互。

领域代码：`helperme/assistant/subagent.py`。子 Session 是普通独立 Session：同一套 `create / advance / recover`，自己的 Journal 与判定。父子关系只存在于 Assistant 侧，用因果事实表达，Runtime 不增加 `parent_session_id` 或 `agent_type`。

## 为什么要有它

**上下文隔离是主要理由，并行是次要理由。** 整个 `helperme/assistant/context/` 都在一条 Session 内部跟上下文膨胀作战——预算、保护窗、体积外置、Level 1 脱水，还欠着 Level 2，见 [上下文](上下文.md)。SubAgent 从另一个方向解同一个问题：让中间过程根本不发生在父的 Journal 里。

所以全部设计围绕一件事——**父只拿结论，不拿过程**。任何一处冒泡都会漏掉隔离，这也是下面那些「不外露」约束的共同来源。

## 类比在哪里会失准

上面那句是准确的接口描述，但「函数」这个类比有三处不覆盖，而这三处正是实现里最容易出错的地方。

**返回值有三种，不是一种。** 父拿到的永远是同一条 `subagent.report` 事实，但它表达三件不同的事：子成功交回结论（`reported=true`，`summary` 有值）；子撞上已识别的失败（`failure` 非空，装 `assistant_failure_message` 的原文）；子静止了但从没调用 `report`（`reported=false`，两个字段都空）。第三种是诚实的「无产出」，不能和失败混为一谈——父是 Judge，重派、换做法还是如实告诉用户，由它读了原因再定。

**「一次性」是投递幂等的性质，不是生命周期的性质。** 子 Session 不会被关闭。它没有 `COMPLETED / TERMINATED`，也不过 Finalization Barrier（见 [Runtime 的“状态与终态”](Runtime.md#状态与终态)），交回结论后就一直停在 `WAITING(user_message)`。真正保证「最多回收一次」的是回收事实的 `delivery_id = f"{child_session_id}:report"`：同一个子第二次回收会被 Journal 的投递幂等吞掉。代价是失败的子即使又被推进并再次静止，第二条终局也不会被看见。

**「传入任务」不是参数传递，是一条事实。** 任务以 `subagent.task` 进入子自己的 Journal，不伪装成用户消息。子因此知道另一端没有人，`report` 是唯一出口。父也没有追问的通道，所以任务描述必须自包含——`delegate` 的参数说明里写明「它看不到当前对话，所需背景必须写在这里」。这是这个接口的真实代价：上下文只能前置。

## 委派与回收

```text
父 Step: delegate(task)
  → 建子 Session（id 取自 command id）
  → subagent.task 进子 Journal
  → Outcome 只说「已创建」
  → 父继续，不阻塞

子独立推进 …
  → report(summary) 或 已识别失败
  → 子静止

Host 观察到静止
  → subagent.report 进父 Journal（requests_decision）
  → 唤醒父
```

委派必须异步。`delegate` 的 Outcome 只是「子 Session 已创建」，结论经外部事实入口回到父。同步版本把一条可能活很久的独立生命线塞进一个 Attempt 的生命周期，进程一崩就得到一个「未知副作用」，而它明明完好地躺在子的 Journal 里。

子 id 从委派命令派生（`{parent}/sub-{command_id}`），重放同一条命令不会造出第二个子 Session。

回收的挂点是 Scheduler 的静止信号。子 Session 是**没有人的 Session**：父落到 `WAITING(user_message)` 合理，因为真的有人会再说话；子落到同样状态则没人会来。由此得到纯机械的判据，不问模型做完没有，只看它还有没有事做：

| `waiting_for` | 含义 |
|---|---|
| `("user_message",)` | 本轮产出完毕，可以回收 |
| 含 `authorization:` | 卡在授权，不是完成 |
| 含 `command:` | 还在跑，继续等 |

Scheduler 因此报告两种终局：静止（`on_quiesced`）与已识别的失败（`on_failed`）。两者语义不同，不合并成一个信号——Scheduler 只诚实说出发生了什么，是否算「一件事做完了」由订阅者判断。漏接 `on_failed`，失败的子就既不静止也不回收，父会拿着一个永远清不空的待回收集合干等。

## 结论不齐时的约束

一个 Step 里可以发多次 `delegate`，子之间只读、互不感知，并行是安全的。但结论不是一次性交付的：每个子静止就单独回收一次，父被逐条唤醒。Runtime 的批次约束只覆盖同一 Step 的工具调用组，跨 Session 回灌的事实不在其中；System Prompt 里也没有任何一句让父等齐子。没有别的机制会替父攒齐。

所以父在决策时从自己已冻结的事实里投影出待回收集合（`project_pending` = 已委派 − 已回收），非空就往 System Prompt 追加一句约束。这个约束只说「还没齐」，不说「还差几个」：没有行为依赖这个数的大小，而带上它会让 System Prompt 每收到一条结论就变一次，整段 prefix 缓存跟着失效。

**「还差谁」是派生值，不冻进事实。** 回收事实只记这个子自己的终局。若把计数算好写进事实，两个并行的子就必须在父维度串行读写才算得对：它们几乎同时静止，各自读父 Journal 再写入，无锁时都读到对方写入前的状态，双双报「还差一个」，父永远等不到 0。改成读时投影，事实只述自己、聚合留给读方，这个约束和那把锁一起消失。

读时投影还保证口径与决策一致：它读的是 `observed_journal_position` 冻结的那份事实，和 Decision Context 同一个边界。重放一次早先的决策，那次子还没回来，约束照旧出现。

唯一要担心的是漏人。`project_delegations` 读的是 `delegate` 的 Outcome，而 Outcome 在 handler 返回之后才提交，handler 里却已经唤醒了子；一个极快的子若在兄弟的 Outcome 落库前就回收，父会看到空集合并据此提前作答。`tests/assistant/test_subagent.py` 造出这个交错来守：脚本模型不阻塞，两个子总是一个跑完再跑另一个，须让 snapshot 与写入都真正让出控制权。在此前提下，凡是已有结论回来的那一帧，兄弟的 Outcome 都已可见。

## 只读边界

`READONLY_TOOL_NAMES` 显式列举子 Session 能看见的全部工具，而不是排除写工具：新增任何工具默认进不来，要进必须有人明确加。`execute_command` 永远不在其中——一条命令是否只读无法静态判断。`delegate` 也不在其中，递归委派因此被同一份名单挡住。名单里没有 MCP 工具，`decision.py` 按 `tool_names` 过滤 schemas，所以子 Session 目前一个 MCP 工具都拿不到。

**只读不是保守选择，是当前唯一的安全边界。** 内置工具全部使用 `ToolSpec.requires_authorization` 的默认值 `False`，只有 MCP 工具与控制面走授权闸；子 Session 一旦拿到写工具就是无闸直写。它不是授权难题的妥协解法。

**单写者是委派树内的不变量，不是全局不变量。** 分界在于并行由谁制造：父决定开几个子，这个并行是 Agent 造的，用户没参与，Agent 必须为它负责；用户同时开两条顶层 Session 是在开两个完整任务，写冲突是用户的选择，Agent 不替他兜底。`get_changes` 的契约因此不需要改——它本就只报告工作区快照、明确不做变更归因；需要约束的只是子 Session 的工具白名单。

这道边界是**安全性质**，所以进程内的父子缓存 `_parents` 必须能从 Journal 认回来，否则重启后子 Session 就没有只读限制了。两个方向都要认：恢复父时从它的 `delegate` Outcome 找出子并唤醒未回收的那些，恢复子时从它自己的 `subagent.task` 事实找回父。只做前者的话，直接 `/resume` 一个子 Session 就绕过了整道边界。

子 Session 换用独立的 System Prompt（`SUBAGENT_PROMPT`）：它没有 Toolset 目录也不碰管理面，两份 catalog 都不拼。

## 对外不可见

子 Session 的 `deliver` 经 `routed_sink` 变成空操作，正文留在子自己的 Journal 里。失败提示同样不外露：`notify` 与 deliver 走同一条路由。用户该看到的是父转述后的判断，而不是一条不知来处的裸错误。父自己失败仍照常送达——拦的是子，不是所有失败。

唯一外露的是一个活动指示：`SubAgentHost` 接受可选的 `activity_sink`，CLI 状态行据此显示「子 Agent 工作中」。它读的是进程内缓存 `_visible_pending` 而不是 Journal 投影，且经 `call_soon` 异步发出——**这条线只管显示，不进入委派与回收的执行闭环**。执行判断始终从 Journal 投影。两者不混用：显示可以丢、可以过期，执行判断不行。

## 不做

- 让子 Session 再委派（递归）
- 让用户看到子的过程正文（只给一个「工作中」的布尔指示），或让子访问父的上下文与 Journal
- 把父子关系写进 Runtime State
- 用 `COMPLETED / TERMINATED` 表达「子做完了」
- 把「还差几个」冻进回收事实
