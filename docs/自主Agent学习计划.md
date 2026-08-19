学习计划顺序文档。

每章节 初次只做核心原型，在后续章节做的时候，发现需要继续补充前边的章节的技术的时候，就进行回顾补充。
如果新 Phase 暴露出旧 Phase 的不足，就回补旧模块。但回补只服务当前 Phase，不做大而全重构。
做每一节任务也是对前面设计的优化过程，必须先处理进行该章节，但未满足的前置模块补充/优化。

## Rule 同步区

- 保持简单、高内聚、低耦合；只为已经出现的真实需求增加抽象。
- Core 只提供稳定的通用机制；可选能力通过 Plugin 组合，删除 Plugin 不应影响 Core。
- 不用静默兜底掩盖契约错误；内部相信契约，只在外部输入边界处理预期错误。
- 明确分离源码、Agent 状态和用户任务数据，并由各自的生命周期管理。
- 完成结论必须基于可验证证据，不能只依赖 Agent 自述。
- 能力执行目录与管理目录分离：disabled 能力不得进入可执行目录，但必须可被 Agent 观察、诊断和提出恢复方案。
- 工具失败只证明本次动作失败；可恢复错误应提供结构化状态和下一动作，Agent 完成恢复后必须重新验证原目标。持久信任状态变更继续经过用户审批。

## 全局路线图

```text
Phase 0 Agent Core
        ↓
Phase 1 Reliable Tool-Calling Runtime
        ↓
Phase 2 TodoList
        ↓
Phase 3 Long-running Agent
        ↓
Phase 4 Agent Application Layer
        ↓
Phase 5 Context Management（完成）
│
├─ ✓ 5.1 Context Projection
├─ ✓ 5.2 Context Budget
├─ ✓ 5.2.1 Tool Result Budget / Runtime Artifact
├─ ✓ 5.3 Safe Compression
├─ ✓ 回补 A / A.1 / B / C
├─ ✓ 5.5 Workspace Sandbox
├─ ✓ 5.6 Workspace Retrieval（工具型）
└─ ✓ 5.7 Command Execution
        ↓
Phase 6 Goal 与能力加载（进行中：6A、6B、6C 完成；6D Skill 待开始）
        ↓
Phase 7 Scheduler / Watcher / Background Task
        ↓
Phase 8 Multi-Agent
        ↓
Memory（后置，外挂）
```

详细执行过程按 Phase 编号见 [`docs/0/`](0/) … [`docs/6/`](6/)。独立于 Phase 的长期架构原则见 [`个人助手的可控扩展：功能演化、边界与抽象成本`](专题/个人助手的可控扩展.md)。

---

## Phase 0 · Agent Core

目标是做一个能够调用工具，并且能够读写文件的 agent 最小 MVP。

### 小节索引

### ✓ Agent Core 最小闭环

- 状态：完成（Benchmark 已达成 2026.06.30）
- 目标：打通最小 agent loop、工具注册/调用与文件读写。
- 结论：已有 Protocol / Message / Registry / Execute / Loop / Workspace / Verification；缺流式、trace 与独立 runtime。
- 详述：[phase0_总结.md](0/phase0_总结.md)

---

## Phase 1 · Reliable Tool-Calling Runtime

把 Agent.run 中混杂的 tool calling loop 抽成可检查、可截断、可停止的 TurnRuntime。

### 小节索引

### ✓ 初版 TurnRuntime

- 状态：完成
- 目标：用 ToolsState 管理一次 Turn 内的工具调用链路，并统一 TurnResult 出口。
- 结论：ToolsState 是账本，TurnRuntime 是执行控制者；上下文压缩与长期会话不属于本阶段。
- 详述：[phase1_总结.md](1/phase1_总结.md)

### ✓ Phase 3 回顾补强（2026.07.14）

- 状态：完成
- 目标：为 interrupt/resume 回补 Tools Runtime 职责边界。
- 结论：拆出 ToolsProtocol / StopGuard / TurnControl；TurnStatus 收敛为 completed/interrupted/blocked/failed。
- 详述：[phase1_Phase3回补总结.md](1/phase1_Phase3回补总结.md)

### ✓ Phase 1 / Phase 4 回补：阶段性说明（2026.08.10）

- 状态：完成
- 目标：保留模型同一 AgentStep 返回的 assistant content 与 tool_calls，并在工具执行前即时输出阶段性说明。
- 结论：不新增阶段解释层；Conversation 保存完整协议事实，TurnProgressSink 只负责对外输出。
- 详述：[phase1_阶段性说明回补总结.md](1/phase1_阶段性说明回补总结.md)

### ✓ Phase 6B 前置回补：异步工具执行链（2026.08.12）

- 状态：完成
- 目标：在 MCP 接入前统一 Application → Session → Turn → Model/Tool 异步主干，同时保持工具批次、Evidence、Artifact 和 StopGuard 语义。
- 结论：Tool handler 收敛为严格 async；异步回补阶段先保持串行，后续已升级为同一 AgentStep 并发执行、原序提交；Application 提供通用异步资源生命周期，`asyncio.run()` 只留在最外层入口。
- 详述：[异步工具执行链回补总结.md](6/异步工具执行链回补总结.md)

---

## Phase 2 · TodoList

让 agent 在长任务前形成可审阅 TodoList，并在执行中通过 `rewrite_todos` 自主维护。

### 小节索引

### ✓ TodoList

- 状态：完成（后续边界与长任务验证已回补）
- 目标：按 Turn 路由 `plain/todo`，把 TodoList 作为柔性行动参考。
- 结论：删除独立 Planner/Replanner；最终回答前必须通过 Todo Sync Barrier。
- 详述：[phase2_总结.md](2/phase2_总结.md)

---

## Phase 3 · Long-running Agent

把一次性 Agent.run 升级成可中断、可继续、可被人类介入的 Session Runtime。

### 小节索引

### ✓ Session Runtime

- 状态：完成
- 目标：支持 session 创建、安全点 interrupt、追加 user_message 后 resume。
- 结论：Conversation 是协议层事实；resume 是同进程继续，不是持久化崩溃恢复。
- 详述：[phase3_总结.md](3/phase3_总结.md)

---

## Phase 4 · Agent Application Layer

建立无状态 `AgentApplication`，让 Console/API 通过显式用例复用同一套 SessionRuntime。

### 小节索引

### ✓ AgentApplication

- 状态：完成
- 目标：拆出应用服务、Composition Root、Prompt 与 Observability 边界。
- 结论：应用层不持有 Session；Channel 持有 session_id 并显式选择 start/resume。
- 详述：[phase4_总结.md](4/phase4_总结.md)

---

## Phase 5 · Context Management

在同一 Session 内管理长期累积的上下文：Conversation 保存完整事实轨迹，运行时生成可发送给模型的安全投影，不更换 Session 身份。

- 状态：完成（2026.08.07）
- 总结：[phase5_总结.md](5/phase5_总结.md)
- 编号说明：原 5.4 Memory Model / Extraction 已后置到 Phase 8 之后；为保持既有文档与提交引用稳定，不重编号 5.5～5.7。

### 路线图

```text
Phase 5.3 完成验收
│
├─ ✓ 回补 A：Dynamic TodoList
├─ ✓ 回补 A.1：Runtime Mode Router
├─ ✓ 回补 B：输入/工具结果边界
└─ ✓ 回补 C：Artifact 生命周期
        ↓
Phase 5.5 Workspace Sandbox
        ↓
✓ Phase 5.6 Workspace Retrieval（工具型）
        ↓
✓ Phase 5.7 Command Execution
        ↓
Phase 6A Goal Loop
        ↓
Phase 6B Toolset Progressive Loading + MCP Toolset Adapter
        ↓
Phase 6C Web 能力的 MCP 验证
        ↓
Phase 6D Skill Progressive Loading
        ↓
Phase 7 Scheduler / Watcher / Background Task
        ↓
Phase 8 Multi-Agent（从 SubAgent Delegation MVP 开始）
        ↓
（很晚，外挂）Memory Model → Memory Extraction → 再考虑 Unified Retrieval
```

### 小节索引

### ✓ 5.1 Context Projection

- 状态：完成
- 目标：建立 Conversation → ModelContext 的最小投影闭环。
- 结论：Conversation 是事实，RuntimeMode 提供控制状态，ModelContext 是二者在某个 AgentStep 上的临时投影。
- 详述：[phase5_1总结.md](5/phase5_1总结.md)

### ✓ 5.2 Context Budget

- 状态：完成
- 目标：正式模型调用发送前检查项目输入预算，并用真实 usage 校准估算。
- 结论：预算作用于完整模型请求，不修改 Conversation；超预算不能靠删除事实轨迹解决。
- 详述：[phase5_2总结.md](5/phase5_2总结.md)

### ✓ 5.2.1 Tool Result Budget / Runtime Artifact（5.3 前置）

- 状态：完成
- 目标：保证单次工具结果有界，超大完整结果外置为 Runtime Artifact 供按需回读。
- 结论：Safe Compression 只处理历史累积，不补救单条无界工具结果。
- 详述：[phase5_2_1总结.md](5/phase5_2_1总结.md)

### ✓ 5.3 Safe Compression

- 状态：完成验收（端到端 Benchmark 已补齐 2026.08.07）
- 目标：在不修改完整事实轨迹的前提下，生成可继续发送给模型的安全投影。
- 结论：Level 1 持续性工具脱水 + Level 2 增量摘要；压缩只更新 ContextState，不改 Conversation。
- 详述：[phase5_3总结.md](5/phase5_3总结.md)

### ✓ 回补 A：Dynamic TodoList

- 状态：完成
- 目标：把固定 Plan 改为 Turn 内可变 TodoList，由 `rewrite_todos` 统一维护。
- 结论：TodoList 是柔性行动参考；最终回答前必须通过 Todo Sync Barrier。
- 详述：[phase5_3_A_Dynamic_TodoList总结.md](5/phase5_3_A_Dynamic_TodoList总结.md)

### ✓ 回补 A.1：Runtime Mode Router

- 状态：完成
- 目标：每个 Turn 按 Conversation 选择 `plain/todo`，不固定整个 Session。
- 结论：Router 只选执行机制；非法路由可降级到 PlainMode，不升级成 Session 失败。
- 详述：[phase5_3_A1_Runtime_Mode_Router总结.md](5/phase5_3_A1_Runtime_Mode_Router总结.md)

### ✓ 回补 B：输入/工具结果边界

- 状态：完成
- 目标：补齐用户输入与单次工具结果的外部边界契约。
- 结论：边界内相信契约，超限明确失败，不交给 Safe Compression 补救。
- 详述：[phase5_3_B_输入工具结果边界总结.md](5/phase5_3_B_输入工具结果边界总结.md)

### ✓ 回补 C：Artifact 生命周期

- 状态：完成
- 目标：明确 Runtime Artifact 为 Session 私有工作抽屉中的外部正文。
- 结论：只有显式 `delete_session` 才整体清理；Turn/Session 结束与 Level 2 裁剪不自动删除 Artifact。
- 详述：[phase5_3_C_Artifact生命周期总结.md](5/phase5_3_C_Artifact生命周期总结.md)

### ✓ 5.5 Workspace Sandbox（完成于 2026.08.05）

- 状态：完成
- 目标：把根目录相对路径解析升级为可配置的多根轻量路径沙箱。
- 结论：Sandbox 只做路径权限边界；存在性与文件操作仍由具体工具负责。
- 详述：[phase5_5_Workspace_Sandbox总结.md](5/phase5_5_Workspace_Sandbox总结.md)

### ✓ 5.6 Workspace Retrieval（工具型）（完成于 2026.08.05）

- 状态：完成
- 目标：在 PathGuard 边界内提供只读回取工具；不自动注入 Context，不改变 Workspace 作为外部事实源的职责。
- 结论：glob 按名称找路径、grep 按内容找匹配行、read_file 按行读取正文；结果有界、截断真实且可继续。
- 详述：[phase5_6_Workspace_Retrieval总结.md](5/phase5_6_Workspace_Retrieval总结.md)

### ✓ 5.7 Command Execution（完成于 2026.08.07）

- 状态：完成；第一版实现、行为测试与真实 Agent Benchmark 均已通过
- 目标：让 Agent 在指定 Workspace 中调用本机 CLI，完成依赖安装、构建、测试、Git 和包管理等真实工程任务。
- 结论：工具适配、PowerShell Runner 与有界捕获分层；Workspace 只约束 cwd，子进程环境显式构造；命令显式声明 `read_only|may_write`，默认保守按 `may_write` 处理并由 StopGuard 要求 get_changes 验证。真实 Agent 已完成发现、安装、失败测试、修改、重测、构建、Git 核对与事实一致总结的闭环。
- 详述：[phase5_7_Command_Execution计划.md](5/phase5_7_Command_Execution计划.md)；[phase5_7_Command_Execution总结.md](5/phase5_7_Command_Execution总结.md)

---

## Phase 6 · Goal 与能力加载

面向更大目标组织任务，并按需渐进加载能力。

### 小节索引

### 6A Goal Loop

- 状态：完成；架构回归与真实模型 Goal Loop benchmark 均已通过。
- 目标：围绕最终目标自动循环完整 Agent Turn，并由独立 Judge 依据冻结 Contract 和真实证据决定完成、继续或暂停。
- 当前边界：Goal 只保存 Objective、Completion Contract、Turn/Judgment 历史和生命周期；Plan/Todo 不进入 Goal 聚合。Contract 自动推导，Executor 不可修改，Judge 只能在 Turn 边界修订 inferred 标准。
- 交互入口：`/goal <objective>` 自动执行 Contract Compilation → Executor → Judge → continuation，直到 completed、paused 或耗尽 `max_goal_turns`。
- 详述：[phase_6A学习.md](6/phase_6A学习.md)

### 6B Toolset Progressive Loading + MCP Toolset Adapter

- 状态：完成；Toolset 渐进加载、MCP Plugin 基础能力、对话审批安装和真实 stdio 闭环 benchmark 已完成
- 目标：按需渐进加载 Toolset，避免一次性暴露全部工具 Schema；接入 MCP，验证外部 Toolset 的发现、选择与调用。
- 前置边界：区分 Plugin 装配、交互命令激活与单次 Turn Capability 注入；6B 只处理 Turn 期 Capability 的选择、加载和释放。
- Turn 约束：TurnInvocation 可覆盖当前 TurntimeMode；受限规划能力使用 PlainMode，避免渐进加载的工具集被 Todo 等模式再次扩张。
- MCP 定位：MCP 是外部 Toolset 的接入协议，不负责能力加载策略；具体适配放在可选 Plugin，Core 不依赖 MCP。
- 当前结论：Core 管理单次 Turn 的加载状态与逐 AgentStep 工具装配，Plugin 通过 `ToolsetProvider` 提供精简目录和具体工具；模型调用 `load_toolset` 后，工具从下一个 AgentStep 可见，Turn 结束后自然释放。
- ToolSpec 前置回补：以 `ToolParameters` 绑定模型 Schema 与运行时校验；支持 Pydantic 与原生 JSON Schema，非法 Schema 和重复工具名直接失败。
- MCP 前置技术债清理：Turn 内 Toolset 在加载时冻结；Application、Session、LLM Client 与 PowerShell 取消/关闭路径闭合；`grep`、`get_changes` 改用异步子进程；JSON Schema 顶层 object 契约前置校验。
- MCP 接入 MVP：管理走 `/mcp` 控制面；Registry/Secret 与 RuntimeState 分离；目录零网络 I/O；`tool_specs` 异步化；工具名 `mcp__{server}__{tool}`；Resources/Prompts 显式读取。
- 有状态 MCP 生命周期回补：Toolset 可见性仍属于 Turn；同一 `(server_id, revision)` 的真实 SDK Client、stdio Server 和领域状态由 Application 级专属 owner task 持有。SDK context 的创建、串行调用与关闭保持在同一 Task；跨 Turn 重新加载不重启 Server，配置变化、取消或 Application 退出时明确关闭。
- MCP 工作目录回补：stdio Server 显式配置 `cwd` 时严格使用该目录；未配置时使用 `~/.helperme/plugins/mcp/runtime/{server_id}`。外部 Server 的日志、截图和临时附件不得因继承 HelperMe 启动目录而污染源码仓库。
- 对话安装回补：Agent 通过多个 Turn 的对话构造单进程 stdio/HTTP Proposal；用户输入 `yes/no`；Application 执行 `disabled → test → enable`。任意持久能力配置变化统一使旧 Session 快照过期，控制面 reload 后由新 Session 捕获最新配置。
- MCP 管理与自纠回补：Console 和 Agent Tool 复用同一 Application Service；Agent 可列出包含 disabled 项的管理目录、真实测试已登记 Server，并为可用的 disabled Server 提交恢复审批。安装与恢复统一复用原子 `test → enable` 用例；人工入口提供 `/mcp retry <id>`，Toolset 执行目录仍只包含 enabled Server。
- 详述：[phase_6B学习.md](6/phase_6B学习.md)；[ToolSpec格式回补总结.md](6/ToolSpec格式回补总结.md)；[MCP前置技术债清理总结.md](6/MCP前置技术债清理总结.md)；[MCP接入设计草稿.md](6/MCP接入设计草稿.md)；[MCP接入实现总结.md](6/MCP接入实现总结.md)；[MCP对话安装与Session能力快照总结.md](6/MCP对话安装与Session能力快照总结.md)

### 6C Web 能力的 MCP 验证

- 状态：完成；Tavily MCP 搜索/提取、Artifact 回读与 Playwright MCP 浏览器交互均已通过真实任务验证。
- 目标：验证 HelperMe 能否通过现有 MCP 安装与渐进加载链路完成公开 Web 的搜索、读取和回答；本阶段不实现原生 `web_search`、`web_fetch` 或 Web Plugin。
- 学习顺序：① 通过 Tavily MCP 完成 search/extract/answer benchmark；② 观察工具发现与加载、结果预算、失败定位和回答质量；③ 只记录真实缺口，不把外部 Provider 的领域语义提前写入 Core。
- 能力边界：搜索、网页读取和浏览器自动化默认属于外部 MCP 的实现责任。HelperMe 只复用通用 MCP 能力管理、Tool Result、TurnEvidence、Conversation 和 Artifact 链路，不要求理解外部能力的内部实现。
- Browser 策略：登录、点击、填写和发布等交互在真实需要时优先接入 Browser MCP。只要 MCP 能完成任务，即不建设自有 Browser Provider、会话模型或自动化框架。
- 重新设计触发：只有真实任务证明 MCP 在关键场景中无法完成目标，例如登录态无法维持、必要交互无法表达、结果无法使用或生命周期与 HelperMe 冲突，才根据已观察到的具体缺口重新设计这一能力。
- 当前结论：Web 是可替换、可移除的外部能力，不是 HelperMe Core 的领域组成。暂停原生 Provider、URL 来源索引、Web 专属安全策略、多供应商切换、fallback、Browser 和 Crawl 等设计。
- 验证结论：现有 MCP 安装、渐进加载、长结果外置、跨 Turn 有状态连接和独立运行目录足以承载当前 Web/Browser 任务，未出现需要自建 Web 能力的缺口。
- 详述：[Web 能力 MCP 接入决策.md](6/Web能力MCP接入决策.md)

### 6D Skill Progressive Loading

- 状态：设计完成；工作区语义与工具路径契约已完成第一版代码回补，待开始实现
- 前置回补：已建立 Environment Selection/Binding、Workspace View、Permission、cwd-relative / Environment-absolute 路径解析、Environment Context 与 EnvironmentLocation；进程级 Sandbox 仍是明确记录的后续能力边界。详见[工作区语义与工具路径契约](专题/工作区语义与工具路径契约.md)。
- 目标：按需发现并加载 Skill 的指令、知识与工作流，避免在 Turn 开始时注入全部 Skill 内容。
- 边界：复用通用 Turn 生命周期与能力快照规则，但不与 Toolset 的目录、加载状态和工具 Schema 数据模型提前合并。
- 当前设计：Agent Workspace 持久安装与 Task Workspace 执行分离；Turn 只注入精简目录，`load_skill` 独占一个 AgentStep 并从下一 AgentStep 完整注入主指令，supporting files 按需读取；脚本继承当前 Turn EnvironmentBinding，并以 Binding.cwd 复用同一命令执行链。安装默认 disabled；更新仅允许用户显式操作或重新部署触发，候选以 hash 冻结，并同时提供模型概括与机器 diff，禁止静默自动更新。
- 详述：[phase_6D学习.md](6/phase_6D学习.md)

---

## Phase 7 · Scheduler / Watcher / Background Task

支持定时、监听与后台任务，让 Agent 不只在同步对话中运行。

### 小节索引

### Scheduler

- 状态：未开始
- 目标：调度定时或延迟任务。
- 结论：（待写）
- 详述：总结待写

### Watcher

- 状态：未开始
- 目标：监听外部变化并触发后续行动。
- 结论：（待写）
- 详述：总结待写

### Background Task

- 状态：未开始
- 目标：在前台会话之外运行后台任务。
- 结论：（待写）
- 详述：总结待写

---

## Phase 8 · Multi-Agent（最后）

多 Agent 协作：先完成单个 SubAgent 的委派闭环，再逐步扩展协调、并发分工与共享状态。

### 小节索引

### SubAgent Delegation MVP

- 状态：未开始（由原 SubAgent 小节后置）
- 目标：定义任务委派与结果回收契约，完成父 Agent 向单个 SubAgent 委派子任务的最小闭环。
- 结论：（待写）
- 详述：总结待写

### Coordinator

- 状态：未开始
- 目标：协调多个 Agent 的目标与分工。
- 结论：（待写）
- 详述：总结待写

### Worker

- 状态：未开始
- 目标：执行被委派的具体工作。
- 结论：（待写）
- 详述：总结待写

### Shared State

- 状态：未开始
- 目标：管理多 Agent 共享状态边界。
- 结论：（待写）
- 详述：总结待写

---

## Memory（后置，外挂）

Long Memory 不进入 Phase 5 主链路，作为后期可选外挂能力。

### 小节索引

### Long Memory

- 状态：后置未开始
- 目标：作为单端可选能力，在 Phase 8 之后或并行外挂：独立 store + 可选注入；出现第二数据源后再做 Unified Retrieval。
- 结论：（待写）
- 详述：总结待写
