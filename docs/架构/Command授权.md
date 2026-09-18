# Command 授权

Command Authorization 是 Web Channel 首版纵向切片，解决「工具副作用需要用户确认」的交互。与 Control Approval（批准安装、更新等管理提案）分离，两者变化原因不同，不抽象成通用「审批框架」。

## 语义

- 授权判定：每个 Command 派发前经授权判定得出 verdict（`allow` / `ask`）。判定是纯函数，只看（tool, args），不读外部状态：
  - 静态工具：spec 上的 `requires_authorization` 布尔，即常量 verdict（默认 `False` = `allow`）。表达「此工具天生有副作用，默认需人确认」；
  - `execute_command`：挂 ExecPolicy，按命令文本前缀规则动态判定（见 [CLI能力](CLI能力.md)）。
  判定函数随工具 Binding 注入；Runtime 只消费 verdict，不理解规则含义。
- Web 总闸 `auto_authorize`：仅 Web 有的 Session 级偏好。表达「这个 Session 我信任 agent，别再问」。没拨过就是关。
- 入口策略：verdict=`ask` 的 Command 是否拦截由当前 owner 入口决定：
  - owner 是 Web：看总闸，没拨过就是关，关则拦；
  - owner 是 TUI：拦截，用 yes/no 一次性处理当前全部待授权命令；
  - 没有 owner：不替人放行。
- 授权是正交的派发 gate：`CommandPhase`（pending / unknown / terminal）只描述执行生命周期，是否派发由 `dispatch_eligible_by_event_id`（授权后设置）与 `authorization_rejected_by_event_id`（拒绝后设置）决定。

## 契约

### 1. 授权判定来源

- 静态：`helperme/tools/builtin/file_manage.py` 的 `write_file`、`helperme/tools/builtin/file_write.py` 的 `apply_patch` / `replace_all` 标 `requires_authorization=True`；只读工具（`read_file` / `glob` / `grep` / `get_changes`）保持 `False`。
- 动态：`execute_command` 由 ExecPolicy 按命令文本判定（默认 `allow`，内置危险前缀清单判 `ask`），不再是「不拦」。

### 2. Web 总闸 `auto_authorize`

- `SessionView.auto_authorize` 只表示 **Web 总闸偏好**，不是当前入口是否正在放行。
- 存储：assistant 层会话元数据（`sessions_root/auto_authorize.json`），**不进 Journal**。模型无需知道。只在人拨过总闸时写入；创建 Session 不写 Channel 默认值。Fork 出的新 Session 未写入。
- `GET /api/sessions/{id}` 不 resume Worker。Host 直接读这份元数据补 `SessionView.auto_authorize`，缺省 `false`。刷新不丢。
- 端点：`POST /api/sessions/{session_id}/auto-authorize`，body `{connection_id, enabled}`（`strict=True, extra="forbid"`），返回更新后的对话投影（内含 `SessionView`）。打开总闸时，当前待授权命令一并 `grant_command`。
- TUI 不读、不写、不暴露这把闸。Telegram / ACP 同样没有总闸入口。
- Worker 是否自动 `grant` 看 **当前 owner**，不把文件里的布尔当全局答案：
  - owner 是 `web:…`：看总闸，没拨过则不放行；
  - owner 是 TUI：不放行，等待 yes/no；
  - 没有 owner：不替人放行。
- 自动放行挂在 `SessionScheduler` 的 quiesce 钩子，也在 Host 同步放行策略时补一次。有 pending 且应当放行则 `grant_command` 并 `wake`；Web 且总闸关闭则广播 `authorization_required`。

### 3. 投影层状态判定

`toolStatusSchema` 由 `running / succeeded / failed / unknown` 扩为增加 `awaiting_authorization`、`rejected`。

对无终态 outcome 的命令，按优先级：

1. 有 `CommandOutcomeReceived` → `succeeded` / `failed`
2. `command_id` 出现在 `CommandRejected` 事件 → `rejected`
3. `command_id ∈ pending_authorization_ids` → `awaiting_authorization`
4. 已派发且会话 idle → `unknown`
5. 其余 → `running`

刷新后等待、拒绝和终态从 Journal 恢复。总闸不在 Journal 里，走第 2 块的元数据。

### 4. 投影层拒绝反馈

`context/projection.py` 的 `_translate_visible_events` 增加 `CommandRejected` 分支，生成一条 `role="tool"`、`tool_call_id=command_id` 的消息，例如：

> 用户拒绝执行该工具调用（command_id=…，工具=write_file）。请勿原样重试；如需继续，请改用其他方式或先向用户解释。

保证每个 tool_call 都有对应 tool 消息（Chat Completions 协议完整），并明确是「用户拒绝」而非「工具失败/超时」。

### 5. Web 授权接口

- `POST /api/sessions/{session_id}/commands/{command_id}/authorize`
- body：`{connection_id, approved}`（`strict=True, extra="forbid"`）
- Host 转发 `resolve_authorization(session_id, command_id, approved)`，只处理单个命令。
- TUI 的 `yes/no` 仍一次性处理当前全部待授权命令（`resolve_authorizations`）。
- 返回更新后的对话投影（内含 `SessionView`）。

### 6. 前端

- `contracts.ts`：`toolStatusSchema` 加两状态；`toolItemSchema` 加 `arguments`；`sessionSchema` 加 `auto_authorize`；新增 `authorization_required` 事件 schema。
- `ExecutionProcess.tsx`：`ToolCard` 加参数展示与「允许 / 拒绝」按钮（仅 `awaiting_authorization` 显示）。
- 浮窗：收到 `authorization_required` → 只在目标 Session 内弹（工具名 + 参数 + 允许 / 拒绝），并重拉该 Session 时间线，避免无正文的写工具卡仍停在「运行中」。
- 输入框下方：Web 总闸开关，切换调第 2 块端点。
- 当前不在该 Session：侧栏沿用未读角标，不另做顶栏。

### 7. `authorization_required` 事件

- SSE 事件名：`authorization_required`
- payload：`{session_id, command_id, name, arguments}`；`execute_command` 被拦时 arguments 中已含完整命令文本，用户可直接看到要确认的对象。（`matched_rule` 命中规则标识后置：等「看不清为何被拦」成为真实问题再加。）
- 广播时机：命令进入等待授权，且当前不应自动放行时；`arguments` 取自对应 `Command.effect.argument_dict()`。

## 设计决策

- 总闸是 Web Session 偏好，不是跨入口的任务事实，所以不进 Journal。
- 创建时不把入口默认值落盘：Web 没拨过就是关。
- 同一条 Session 从 Web 转到 TUI 再转回 Web，仍用 Web 自己那份总闸。
- TUI 从「全放行」改为「拦截 ask」：ExecPolicy 是防误操作的软边界，误操作在任何入口都是误操作；TUI 已有 yes/no 交互，机制无需新增。注意此变化使 `write_file` 等静态授权工具在 TUI 也开始拦截。

## 拒绝语义

用户拒绝某次工具执行，**不是**把工具从 `surface.schemas` 移除。模型仍看得到工具，只是明确知道「这次调用被用户拒绝」，从而避免困惑、不会原样重试。
