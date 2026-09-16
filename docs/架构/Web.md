# Web

Web 是与 TUI、ACP、Satori 平级的原生 Channel。浏览器只通过 Web 后端使用
Assistant 应用操作和查询，不直接读取 Journal，也不承担 Session 恢复。

## 当前切片

界面有 Session 侧栏、当前会话时间线、输入框和停止按钮。时间线按用户轮次展示
Step 与其工具调用。页面级 SSE 与当前选中的 Session 解耦：切换只改变 Host owner 的
选择和中间栏，不取消仍在运行的 Session，也不打断后台工具。

后台 Session 完成后可以从 Journal 投影恢复最终正文和工具终态；preview 与运行中
的工具进度仍是可丢的进程内展示，刷新后分别由 `output_final` / Journal 补齐。

```text
输入消息
→ POST /api/sessions/{id}/inputs
→ accept_input(delivery_id, text)
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
activePreview、unread、committed cache 与按 `command_id` 索引的 live tools；
组件本地状态只保存输入框、滚动与侧栏开合等纯 UI 状态。
Session 运行或存在活动 preview 时，时间线跟随内容尺寸变化固定到最底部；运行结束后
解除跟随，用户可以自由查看历史。

## 查询投影

- `list_sessions()` 从各 Journal 投影顶层 Session 摘要，不列出 SubAgent Session。
- `GET /api/sessions/{id}` 只从 Journal 投影时间线，不 `resume` Worker。刷新后
  的历史恢复走这条读路径；`POST .../select` 只在页面 SSE 连上后绑定 Host owner。
- `conversation(session_id)` 投影统一时间线 `items`：`UserMessageReceived` 为
  用户消息；每个 `StepCommitted` 投影为一个 Step，其中嵌套正文和该步的非
  `deliver` Command。未传入 Worker `SessionView` 时，从同一份 Journal 重放得到只读视图。
- Step 使用 `step_id` 作为身份，并沿用 `step.trigger_event_id` 作为独立
  `output_id`；preview 不进入历史投影。
- Step 内工具使用 `command_id` 作为身份。终态来自 `CommandOutcomeReceived`：成功或
  失败（含错误信息）。尚无 Outcome 时：`PENDING` 或 Session 仍在运行则为运行中；
  Attempt 已开始（`UNKNOWN`）且 Session 已 idle 则为中断（结果未知）。这是 Channel
  展示，不把未知 Attempt 写成 Outcome。`deliver` 不进入时间线。

## 页面连接与实时事件

`GET /api/events` 建立页面级 SSE，并分配瞬时 connection identity。该 identity
映射为 Host owner；SSE 断开时释放 owner，但不取消任何 Session，也不使
`deliver` 失败。新建或选择 Session 时由 Web Channel 调用
`select(owner, session_id)`。

Event Hub 向所有页面连接广播带 `session_id` 的事件，每个 Session 只保存一个
活动 preview。Host 在 busy/idle 转换时发送 `session_activity`。最终正文走
`output_final`，与 preview 共用 `output_id`，前端不得重复显示。

`tool_progress` 只携带 `command_id`、工具名和状态，不广播工具参数或返回值。
前端用 `command_id` 合并实时事件与 Journal 卡片：Journal 成功/失败终态优先；
Journal 仍为运行中或中断时采用更新的实时状态。切换 Session 只改当前视图，不停止其他 Session
上的工具。

前端按事实做两层展示归约：有工具的已提交 Step 属于执行过程，每个 Step 可独立
折叠；流式 preview 先在框外按正文样式展示。Step 提交后，无工具则原地成为最终
回复，有工具才移入执行过程。最终回复出现后，整个执行过程自动折叠。preview、
committed cache 和 Journal Step 使用同一 `output_id` 作为显示身份，阶段切换不重复
挂载普通最终回复。助手正文由 `@ai-markdown/react-mantine` 渲染，网络增量先经
`useSmoothStream` 平滑释放，并用 Remend 修复尚未闭合的流式 Markdown 尾部。

## 编辑消息与历史分支

用户消息不原地修改。`POST /api/sessions/{id}/forks` 以目标
`UserMessageReceived` 之前的完整事件前缀创建新 Session，随后把编辑后的文本作为
新事实写入并执行。源 Session 是持续事件流，无须停止或结束；即使它仍在推进，已经
提交的历史前缀也保持不可变，源分支可以继续独立运行。原 Session 保持不变，Web 自动
选择新分支。

分支创建时物化完整事件前缀，并重建新 Session identity 对应的 Step basis；同时复制
前缀可能引用的 Artifact 与附件抽屉。因而上下文、ToolSurface、动态加载的 Toolset、
管理域、Skill 使用记录和 Compact 历史都从同一份 Journal 恢复，不存在只继承消息的
旁路。截止点后的 Step、工具结果和能力加载不进入新分支，已经完成的历史 Command 只
作为事实重放，不重新执行。编辑后的消息保留原消息的附件引用。

本切片不包含授权卡片、SubAgent 展示或 Session 管理。



## 那下一片应做 **授权交互**。当前工具卡把“等待授权”误显示成“运行中”，这是现有闭环里最明显的语义缺口。

建议只完成这一条纵向链路：

- 工具卡增加 `awaiting_authorization`、`rejected` 状态。
- 卡片展示工具名和调用参数，提供“允许 / 拒绝”按钮。
- 按 `command_id` 单独决策，不再一次处理全部待授权命令。
- Web 增加明确的授权接口，不把按钮转换成聊天消息里的 `yes/no`。
- 授权后原卡片直接转为运行中，切换 Session 仍不影响执行。
- 刷新后从 Journal 恢复等待、拒绝和最终状态。
- 增加实时的“需要授权”通知，否则无正文输出的工具调用可能不会触发页面刷新。

同时要保持两种审批分离：

- **Command Authorization**：允许某次工具产生副作用。
- **Control Approval**：批准安装、更新等管理提案，展示 `summary` 和 `risk`。

本切片先做 Command Authorization；Control Approval 下一片再接。不要抽象成通用“审批框架”，两者变化原因不同。
