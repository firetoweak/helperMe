# Runtime

内核在 `helperme/runtime/`。它不认识 MCP、Skill、Goal、Todo、会话产品词。

## Stream

一条可独立排序、推进、等待、恢复的执行生命线。同一时刻最多一个 Step 在为该 Stream 做模型决策。

Runtime Core 不负责选择 Stream。Channel 或 Automation 生成或选择 Stream identity；identity 给定后，`AssistantStreams` 调用幂等的 `create_stream(identity)`，Core 持久化这条空执行生命线，并负责后续 Event 持久执行、State 重建、历史重放以及按 Command Contract 进行机械恢复。Stream 的存在不以“已经有 Event”为条件；创建本身不是 Event，也不触发模型或 Step。Core 不理解它对应 Session、Conversation、任务、后台工作还是 SubAgent。

## Event 与 Journal

Event 记录已经确认发生的事，不可变，只能追加。它不解释事实，也不决定下一步。

Journal 是顺序与语义权威。大正文在 Artifact Store；Journal 持稳定引用。Replay 重放 Event，不调用模型或工具。

`helperme/runtime/journal/api.py` 定义协议、Lease 与 `MemoryJournal`；`journal/sqlite.py` 实现控制台使用的 `SqliteJournal`。Journal 只负责事实持久化与机械原子性，不承载 State 或决策语义。

## State

Canonical State 由 Event 归约得到，不是先改内存再补日志。Runtime Status 由 State 确定：

| 状态 | 含义 |
|---|---|
| RUNNABLE | 有待消费的决策触发，可以 `advance()` |
| WAITING | 等人、等授权、等 Command 结果 |
| COMPLETED | 完成，且过了 Completion Barrier |
| TERMINATED | 终止，且过了 Termination Barrier |

`waiting_for` 例如 `user_message`、`authorization:{command_id}`、`command:{command_id}`。

## Step

决策闭环。一次 Step 消费一个触发（UserMessage、工具结果、请求决策的 Domain Fact、被拒绝的 Command 等），产出 `ModelDecision`：文本、Command 列表、可选 `LifecycleIntent`（none / complete / terminate）。

Decision Context 在 Step 开始时冻结模型所见的 Event、Criteria、Prompt、Tool/Skill schemas。模型调用期间的新事实只进入后续 Step。Commit Guard 在提交时重新验证 Runtime 当前真实世界中的 claim、trigger、basis version 与终态等不变量；冻结视图不能替代提交校验。

Runtime 不替模型做语义判断。它只接收 `ModelDecision`，并确定性检查该 Decision 能否提交、Command 能否派发。Assistant 决策边界把本次精确请求、模型配置、Projector 版本和原始响应保存为 Replay Manifest；Core 只随 `StepCommitted` 保存不透明 `artifact_refs`，不解析 Prompt、Schema 或模型协议。

同 Step、`decision_on_outcome=True` 的并行 Command 构成无序集合。调用、开始、完成和 Outcome 写入顺序都没有决策语义；全部终态后才形成下一次决策（sibling join）。`UserInterruptReceived` 不受该屏障约束。

`decision_on_outcome` 是 Command 签发时冻结的机械调度事实，默认 `True`。该值由 Core 外的 Tool Binding 提供；Core 只读取，不根据工具身份、Command 类型或 Outcome 内容推断。目前唯一显式例外是 `deliver=False`。

Journal 仍按真实接纳顺序记录每个 Outcome，用于审计和并发裁决，但 Runtime 不为并行结果顺序建立额外语义，也不因单个结果到达触发模型。

Assistant 向模型序列化同一段相邻并行结果时，按 Command 签发顺序做稳定展示，避免完成竞速改变模型输入；这只是投影规范化，不改写 Journal，也不赋予该顺序业务含义。Interrupt 形成的上下文分段不被跨越重排。

Runtime 不推断后来的普通 `UserMessageReceived` 会使既有决策输入失效。闭合的 Command 结果组与后续 UserMessage 按各自顺序分别触发 Step；只有显式 `UserInterruptReceived` 使用中断优先规则。

CLI 的并发输入由 Channel 映射为 Interrupt，并通过 `AssistantStreams` 写成 `UserInterruptReceived`；`Ctrl+C` 只退出 CLI 进程，不进入 Runtime。

## Domain Fact

产品领域事实通过 `DomainFactCommitted(fact_type, data, requests_decision)` 进入 Journal。Runtime 只校验通用载荷并读取 `requests_decision` 调度位，不解释 `fact_type` 和 `data`。Criteria、Judgment 等类型、编码和投影属于 Assistant Completion；新增或删除它们不修改 Runtime Event、Codec 或 Reducer。

不是每种连续交互状态都要新增 Domain Fact。若领域状态能由既有 Event 无歧义投影，就直接复用它：例如 Toolset 激活由成功的 `load_toolset` Command Outcome 投影，并由 Assistant ToolSurface 恢复连接。Runtime 不认识 Toolset，也不保存其缓存。

## Command

副作用闭环。Dispatcher 按结果到达顺序写回 Event。Host Binding 声明的授权要求在 Command 签发时冻结；需要人批准的 Command 在 dispatch 前等待 `CommandAuthorized`。Runtime 不按工具名或参数推断风险，模型也不能给自己授权。

`OutcomeStatus` 描述执行适配器报告的调用层终态，不解释返回正文。普通工具即使返回 `{ "ok": false, "code": ... }`，Runtime 也只把完整 value 交给后续 Step；它不能读取领域字段后自行改写 Outcome、重试、终止或选择替代方案。需要调用层 `FAILED` 时，由 Tool Adapter 显式返回对应 `ToolTerminal`。

预期内失败必须由 Tool Adapter / handler 转换成确定 Outcome。只有异常逃逸到 Runtime 边界、且没有可靠最终结果时，Attempt 才保守地停在现有 `unknown`；不新增更细异常状态，也不把“抛异常”武断等同于“外部动作失败且无副作用”。

未预期异常必须原样穿透当前调用链，Host 不得为了继续运行而宽泛捕获。异常发生前已持久化的 Attempt 仍保持 `unknown`；下次显式恢复 Stream 时才启动现有 Recovery Contract。Runtime 能查询就记录查询事实；不能确认就追加 `CommandRecoveryRequired`。这只是恢复历史中的不确定执行，不是把程序 bug 改写成业务错误。

`bind_tool` 允许 Host 在 Stream 进行中补 Binding。Runtime 不解释工具从哪来。

恢复同样遵守“Runtime 不替模型决定”：Dispatcher 可以按 Tool Recovery Contract 查询外部事实；查到终态就记录 Outcome，确认 Attempt 从未产生外部效果则继续派发原 Command。若结果仍是 `unknown`，Runtime 只写 `CommandRecoveryRequired` 及契约允许的选择，不自行 retry、abandon 或 cancel。选择由后续模型 Step 或用户作出。

## 终态

`LifecycleIntent.complete` 只是模型提交的完成声明，不是 Runtime 对任务的判断。`advance()` 不再自动终态化；Host 先运行 Judge / Policy，再显式调用 `finalize()`。Core 的 Finalization Barrier 只原子验证没有未消费的决策输入和必要依赖，不能判断目标是否满足。

`LifecycleIntent.terminate` 与 `/stop` 同样留下显式声明并经过相应终态边界。abandon ≠ cancel。

## 不是 Runtime 的事

Turn、Conversation、Context 窗口、Trace、判定标准文本、MCP Server 列表、Skill 目录：都是投影或 Host 策略。细节见 [状态推进模型](Runtime状态推进模型.md)。
