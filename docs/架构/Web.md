# Web

Web 是与 TUI、ACP、Satori 平级的原生 Channel。浏览器只通过 Web 后端使用
Assistant 应用操作和查询，不直接读取 Journal，也不承担 Session 恢复。

## 当前切片

界面有按工作区分组的 Session 侧栏、当前会话时间线和输入框。每条会话必须带
`workspace_id`；没有归属的存量会话不出现在列表里。没有工作区时顶部「新建会话」
不可用，界面引导先创建。顶部「新建会话」落到最近一次聊天的工作区，若还没有带
归属的会话则落到最近创建的工作区；分组上的 `+` 只在该工作区建草稿，路由是
`/workspaces/:workspaceId`。尚未发出用户消息时，同一工作区再点「新建会话」仍是
那条草稿；发出第一条用户消息后这条 Session 锁定。侧栏只列出已经锁定的会话，
分组内默认最近 5 条，其余收进 More。草稿可以贴图，但不因此锁定。
输入框底部展示当前工作区路径、逻辑模型、输入上下文占用，以及 compact 次数与相位；路径只读，提醒人现在落在哪个沙箱里。Composer 的图片入口：加号打开文件选择、
粘贴或拖入图片后以缩略图 tile 挂在输入框上，不把 `[Image #n]` 写进可见正文。发送时
Channel 仍按既有附件契约写入 token 与 `artifact_refs`。时间线按用户轮次展示 Step 与其工具调用。页面级 SSE 与当前选中的
Session 解耦：切换只改变 Host owner 的选择和中间栏，不取消仍在运行的 Session，也不
打断后台工具。

后台 Session 完成后可以从 Journal 投影恢复最终正文和工具终态；preview 与运行中
的工具进度仍是可丢的进程内展示，刷新后分别由 `output_final` / Journal 补齐。

```text
点「新建会话」或分组 `+`：若该工作区已有未锁定草稿则进入它，否则 POST /api/sessions（必须带 workspace_id）
→ 空白时间线（Journal 尚无 UserMessageReceived，不出现在侧栏）
→ POST /api/sessions/{id}/attachments（若有图；不锁定）
→ POST /api/sessions/{id}/inputs  （text 含 [Image #n]，artifact_refs 为附件 id）
→ accept_input(delivery_id, text, artifact_refs)
→ Session 锁定，进入侧栏
→ session_activity: running
→ StepCommitted 投影助手正文与非 deliver 工具卡
→ tool_progress: running / succeeded / failed
→ preview started / delta
→ output_final
→ session_activity: idle
→ 刷新后从 Journal 恢复 committed 正文与工具历史
```

```text
React UI ── HTTP / SSE ── Web Channel / Event Hub ── Assistant / Host ── Journal / Worker
```

前端采用 React、TypeScript、Vite、Mantine、`@ai-markdown/react-mantine`、Redux
Toolkit、RTK Query、React Router 和 Zod。Mantine 只提供布局与视觉组件；聊天界面可参考 assistant-ui 的
交互样例，但不接入其 Runtime、Thread 或 Adapter 模型。RTK Query 保存服务端投影；独立 runtime slice 按 Session 保存 activity、
activePreview、unread、committed cache、按 `command_id` 索引的 live tools，以及当前未锁定草稿
和当前页面 owner 绑在哪条 Session 上。
组件本地状态只保存输入框、滚动与侧栏开合等纯 UI 状态。
Session 运行或存在活动 preview 时，时间线跟随内容尺寸变化固定到最底部；运行结束后
解除跟随，用户可以自由查看历史。

运行中输入框显示「暂停」：当前 Step 和已派发 Command 继续跑完，之后 Scheduler
不再因 outcome 自动进入下一拍。`should_wake` 且非 running 时显示「继续」：
人按过暂停、或 Worker 已不在但 Journal 仍是 RUNNABLE，都走同一颗按钮。
点「继续」调用 `POST .../retry`（若暂停则顺手清掉暂停并 wake；否则只
`resume` / wake），不写用户消息。Agent 自然停在等用户输入时
`should_wake` 为 false，两按钮都不出现。这不是 `cancel_turn`。
有 `lastError` 时横幅「再试」取代「继续」，仍走同一条 retry。
`SessionView.auto_authorize` 只活在 Host 的 `sessions_root/auto_authorize.json`，
**不进 Journal**，Worker 不读不写该文件。缺省 false，只在人拨过时由 Host 写入；
创建和 Fork 不写。Worker 只收 Host 推来的 preference/grant。
`SessionView.paused` 只活在 Host 的 `sessions_root/paused.json`，**不进 Journal**，
Worker 不读不写该文件。缺省 false，只在人拨过时由 Host 写入；创建和 Fork 不写。
Worker 每次推进前向 Host 询问 `is_paused`；`resume` 在暂停时只 `view`，不 wake。
`select` 绑定 owner；仅当 Journal 仍 `should_wake` 且未暂停时才启动 Worker 并
wake。空闲 WAITING 不拉起进程，也不把应用请求报成运行中。刷新走 GET 投影，Host
直接补字段，不 resume Worker。新用户消息由 Host 清掉暂停。端点
`POST /api/sessions/{id}/paused`，body `{connection_id, paused}`。

运行中第一次发送不立刻入账：正文和图片停在输入框上方。当前轮回到 idle 后按顺序发出；点「立即发送」马上走现有后到消息；点垃圾桶把停放内容退回编辑栏，不是丢掉。这不是 `cancel_turn`，也不另建后端队列。刷新或切换会话丢弃停放。

## 查询投影

- `list_sessions()` 从各 Journal 投影顶层 Session 摘要，不列出 SubAgent Session、
  尚无 `UserMessageReceived` 的空 Journal，以及没有 `workspace_id` 的存量会话。
  `SessionSummary` 含 `workspace_id`。
- `GET /api/workspaces` / `POST /api/workspaces` 读写 `~/.helperme/workspaces.json`。
  创建时路径必须是已存在目录，且不能与已有工作区完全相同。
- `GET /api/sessions/{id}` 只从 Journal 投影时间线，不 `resume` Worker。刷新后
  的历史恢复走这条读路径；`POST .../select` 只在页面 SSE 连上后绑定 Host owner。
- `conversation(session_id)` 投影统一时间线 `items`：`UserMessageReceived` 为
  用户消息；每个 `StepCommitted` 投影为一个 Step，其中嵌套正文和该步的非
  `deliver` Command。未传入 Worker `SessionView` 时，从同一份 Journal 重放得到只读视图。
  Host 另补 `compact_count` / `compact_phase`，不 resume Worker。
- Step 使用 `step_id` 作为身份，并沿用 `step.trigger_event_id` 作为独立
  `output_id`；preview 不进入历史投影。
- Step 内工具使用 `command_id` 作为身份。终态来自 `CommandOutcomeReceived`：成功或
  失败（含错误信息）。尚无 Outcome 时：`PENDING` 或 Session 仍在运行则为运行中；
  Attempt 已开始（`UNKNOWN`）且 Session 已 idle 则为中断（结果未知）。这是 Channel
  展示，不把未知 Attempt 写成 Outcome。`deliver` 不进入时间线。

## 页面连接与实时事件

`GET /api/events` 建立页面级 SSE，并分配瞬时 connection identity。该 identity
映射为 Host owner；SSE 断开时释放 owner，但不取消任何 Session，也不使
`deliver` 失败。空闲时每 15 秒发一条 SSE 注释心跳，避免长思考期间代理因
无事件断开页面连接；注释不进前端状态。新建或选择 Session 时由 Web Channel 调用
`select(owner, session_id)`。点「新建会话」时：当前已是该工作区的未锁定草稿则保持；
否则复用该工作区已有未锁定草稿，或 `create` 一个新的。草稿按工作区各留一份，
分组 `+` 不会掉进别的工作区的草稿。发出第一条用户消息后草稿锁定。

Event Hub 向所有页面连接广播带 `session_id` 的事件，每个 Session 只保存一个
活动 preview。Host 在 Scheduler 真正开始/结束推进时发送 `session_activity`，
不因 `select`、授权策略或 `view` 这类应用请求报 `running`。前端收到
`session_activity` 后重拉该 Session 投影，让 `should_wake` 与工具终态跟上
Journal。已识别的模型失败
走 `session_failed`，记在 Session 的瞬时 `lastError` 上，用输入框上方提示展示，
不进 `output_final`、committed cache 或 Journal。提示旁「再试」调用
`POST /api/sessions/{id}/retry`，只 `wake` 当前 Session：不写用户消息、不 fork、
不动 Journal；若当时暂停则顺手清掉暂停。下一次 `session_activity: running` 清掉
失败提示；同一 Session 再次失败则覆盖。最终正文走
`output_final`，与 preview 共用 `output_id`，前端不得重复显示。输入框底部展示
当前逻辑模型名，以及该 Session 的输入上下文占用：请求前为估算值，响应后为
LLM 返回的实际 input tokens，分母为配置的 `model_context_limit`。占用随
`context_usage` 实时更新，刷新后回到 0，直到下一次决策。
Compact 状态来自 Host `ConversationStatus`：`GET /api/sessions/{id}` 带
`compact_count` / `compact_phase`，变化走 SSE `conversation_status`。
`running` / `ready` / `failed` 对应整理中、等待切换、失败。输入框底部写次数和相位；
`failed` 时在输入框上方单独横幅说明窗口仍超预算则无法继续推进，不进
`lastError`，也不用「再试」。压缩 reader 进程失败不写成用户会话的
`session_failed`，只把源会话标成 compact 失败。

`tool_progress` 只携带 `command_id`、工具名和状态，不广播工具参数或返回值。
前端用 `command_id` 合并实时事件与 Journal 卡片：Journal 成功/失败终态优先；
Journal 仍为运行中或中断时采用更新的实时状态。切换 Session 只改当前视图，不停止其他 Session
上的工具。

前端按事实做两层展示归约：有工具的已提交 Step 属于执行过程，每个 Step 可独立
折叠。Step 标题回答“这一步在做什么”，而不是“调了哪些工具”：优先展示该步的意图正文
（`decision.content` 的首个非空行，截断到一行）；没有意图正文时退回调用预览——工具名加第一个
字符串参数，与展开态里工具的紧凑写法一致；该步仍在输出思考、尚无正文与工具时显示「思考中」。
标题只放一行，完整正文在展开态给出（正文排在思考块之前）。
所有 Step 默认折叠，标题承担“在做什么”的表达；要分析执行细节（思考块、工具与参数）时由用户
自己展开，只有等待授权这类需要用户操作的状态才自动展开。工具参数默认收起，Step
结束后自动折上，待授权时展开。流式 preview 先在框外按正文样式展示。当前轮次已在运行、还没有正文或
进行中的工具时，显示「思考中」。模型 `reasoning_content` 增量走独立的
`thinking.started` / `thinking.delta` / `thinking.finished`，落到可折叠的思考块，
与 preview / `deliver` 正文分开。思考块只展示纯文本，流式时展开、结束后自动折上。刷新后可从 Step 的
`decision_metadata.message_extensions.reasoning_content` 恢复已提交的思考。
Step 提交后，无工具则原地成为最终
回复，有工具才移入执行过程。最终回复出现后，整个执行过程自动折叠。preview、
committed cache 和 Journal Step 使用同一 `output_id` 作为显示身份，阶段切换不重复
挂载普通最终回复。助手正文由 `@ai-markdown/react-mantine` 渲染；preview 增量按动画帧
合并后再进 Redux，流式期间不做代码高亮，结束后再高亮。Remend 只修尚未闭合的
流式 Markdown 尾部。思考块只展示纯文本，流式时展开、结束后自动折上。

## 编辑消息与历史分支

用户消息不原地修改 Journal。时间线上点编辑后在气泡位置改字，发送走
`POST /api/sessions/{id}/forks`：以目标 `UserMessageReceived` 之前的完整事件前缀
创建新 Session，随后把编辑后的文本（可与原文相同）作为新事实写入并执行。原文重发
与改字是同一条 fork，不是第二条协议。源 Session 是持续事件流，无须停止或结束；即使
它仍在推进，已经提交的历史前缀也保持不可变，源分支可以继续独立运行。原 Session
保持不变，Web 自动选择新分支。前端路由和侧栏仍停在用户原来点开的那一栏：编辑发出后
立刻去掉该消息之后的渲染，新输出在原地开始。被 fork 出的新 identity 不作为新会话
出现在顶层列表。Journal 仍是新 identity，不是改写旧会话。

分支创建时物化完整事件前缀，并重建新 Session identity 对应的 Step basis；同时复制
前缀可能引用的 Artifact 与附件抽屉。因而上下文、ToolSurface、动态加载的 Toolset、
管理域、Skill 使用记录和 Compact 历史都从同一份 Journal 恢复，不存在只继承消息的
旁路。截止点后的 Step、工具结果和能力加载不进入新分支，已经完成的历史 Command 只
作为事实重放，不重新执行。编辑后的消息保留原消息的附件引用。

Command 授权已按 [Command 授权](Command授权.md) 落地。

父时间线把 `delegate` / `reclaim` 从普通工具卡分出，作为 SubAgent 调用入口。
`delegate` 的命令终态仍是派出回执，不改写成子生命线。父 idle 且
`has_active_subagents` 时，当前轮次在「思考中」同一层画「子 Agent 执行中」；
这是 Channel 活动指示，不把父标成 `running`，也不改变 Host「父可以先退、
报告再唤醒父」的生命周期。当前仍不展示子 Session 过程或 `subagent.report`
正文。这张卡是以后展开子思考、工具与结论的埋点。不包含 Session 管理。



## 下一片

Command Authorization 已落地，契约见 [Command 授权](Command授权.md)。

还没接的是 **Control Approval**：批准安装、更新等管理提案，展示 `summary` 和 `risk`。
不要和 Command Authorization 抽象成通用审批框架，两者变化原因不同。
