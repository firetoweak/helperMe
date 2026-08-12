学习计划顺序文档。

每章节 初次只做核心原型，在后续章节做的时候，发现需要继续补充前边的章节的技术的时候，就进行回顾补充。
如果新 Phase 暴露出旧 Phase 的不足，就回补旧模块。但回补只服务当前 Phase，不做大而全重构。
做每一节任务也是对前面设计的优化过程，必须先处理进行该章节，但未满足的前置模块补充/优化。

## Rule 同步区

- 禁止为了跑通当前局部模块而添加静默兜底、隐式默认值或自动生成关键关联数据。兜底不得掩盖上层调用错误，否则会破坏整体设计并显著增加调试成本。
- 可选能力只能依赖 Core 的公共端口；Core 不得引用具体 Plugin。Plugin 引出的 Core 改动必须具有与插件领域无关的通用语义。移除某个 Plugin 后，Core 应无需修改并能独立运行。
- Plugin 是构建在 Core 公共端口之上的 Agent 辅助支架，负责可选的领域工作流、外部能力接入及其状态；它可以组合 Core 能力，但不得把自己的领域名词、生命周期或存储模型写回 Core。判断边界时不看能力是否“常用”，只看删除该 Plugin 后 Core 是否仍能独立运行。
- 源码仓库、Agent Workspace 与用户任务 Workspace 必须分离。源码仓库只保存实现；`~/.helperme` 是 Agent Workspace，保存 Session Artifact、Plugin 安装内容和 Agent 状态；用户任务 Workspace 由配置指定。Agent Workspace 不进入普通文件工具的 Workspace Roots，Plugin 只能通过自身受控端口访问其专属目录。
- Plugin 的持久安装与 Run 期加载是两个生命周期：安装结果保存在 Agent Workspace；单次 Run 只按需加载已安装能力，并在 Run 结束后释放临时加载状态。协议适配器只负责外部能力发现与调用，不负责 Core 的能力选择策略。
- 工具参数描述与运行时校验必须由同一个 `ToolParameters` 契约提供。外部 JSON Schema 原样进入通用 Core 契约；MCP Plugin 禁止通过动态生成 Pydantic Model 或静默改写 Schema 绕过该设计。
- 文件工具访问上限由 `model_config.yaml` 在 Composition 阶段确定，Application 创建后不可变；模型、Run、Skill 与 Plugin 均不得自行升级。`host` 只取消应用路径范围限制，不能绕过操作系统权限，也不等同于命令沙箱。
- 面向应用的超参数统一由 `model_config.yaml` 提供。默认 Run 轮次上限注入 AgentApplication，普通 Run 与 Plugin Run 均通过 RunHost 解析同一默认值；下层 Runtime 只保留内部调用所需的显式覆盖能力，不维护面向用户的配置入口。
- Goal 保存 Objective、版本化 Completion Contract、Turn/Judgment 历史与生命周期；Plan 与 TodoList 保持为单次 Turn 内的柔性认知工具。
- Completion Contract 自动推导并在每个 Executor Turn 开始前冻结。Executor 无权修改；Judge 只能在 Turn 边界修订 inferred 标准和验证方法，不能自动新增、删除或改写 user 标准。
- Goal 完成必须由隔离 Judge Run 判断。`done` 必须引用证据；结构化命令和 Workspace 要求由 CompletionGate 读取 Judge Run 的真实 RunEvidence 核验，Executor 自述不能替代完成事实。

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
Phase 6 Goal、能力加载与委派（进行中：6A 已完成，下一步 6B）
        ↓
Phase 7 Scheduler / Watcher / Background Task
        ↓
Phase 8 Multi-Agent
        ↓
Memory（后置，外挂）
```

详细执行过程按 Phase 编号见 [`docs/0/`](0/) … [`docs/6/`](6/)。

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

把 Agent.run 中混杂的 tool calling loop 抽成可检查、可截断、可停止的 RunRuntime。

### 小节索引

### ✓ 初版 RunRuntime

- 状态：完成
- 目标：用 ToolsState 管理一次 run 内的工具调用链路，并统一 RunResult 出口。
- 结论：ToolsState 是账本，RunRuntime 是执行控制者；上下文压缩与长期会话不属于本阶段。
- 详述：[phase1_总结.md](1/phase1_总结.md)

### ✓ Phase 3 回顾补强（2026.07.14）

- 状态：完成
- 目标：为 interrupt/resume 回补 Tools Runtime 职责边界。
- 结论：拆出 ToolsProtocol / StopGuard / RunControl；RunStatus 收敛为 completed/interrupted/blocked/failed。
- 详述：[phase1_Phase3回补总结.md](1/phase1_Phase3回补总结.md)

### ✓ Phase 1 / Phase 4 回补：阶段性说明（2026.08.10）

- 状态：完成
- 目标：保留模型同轮返回的 assistant content 与 tool_calls，并在工具执行前即时输出阶段性说明。
- 结论：不新增阶段解释层；Conversation 保存完整协议事实，RunProgressSink 只负责对外输出。
- 详述：[phase1_阶段性说明回补总结.md](1/phase1_阶段性说明回补总结.md)

---

## Phase 2 · TodoList

让 agent 在长任务前形成可审阅 TodoList，并在执行中通过 `rewrite_todos` 自主维护。

### 小节索引

### ✓ TodoList

- 状态：完成（后续边界与长任务验证已回补）
- 目标：按 Run 路由 `plain/todo`，把 TodoList 作为柔性行动参考。
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
Phase 6B Skill / Toolset Progressive Loading
        ↓
Phase 6C SubAgent Delegation
        ↓
Phase 7 Scheduler / Watcher / Background Task
        ↓
Phase 8 Multi-Agent
        ↓
（很晚，外挂）Memory Model → Memory Extraction → 再考虑 Unified Retrieval
```

### 小节索引

### ✓ 5.1 Context Projection

- 状态：完成
- 目标：建立 Conversation → ModelContext 的最小投影闭环。
- 结论：Conversation 是事实，RuntimeMode 提供控制状态，ModelContext 是二者在某个 Round 上的临时投影。
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
- 目标：把固定 Plan 改为 Run 内可变 TodoList，由 `rewrite_todos` 统一维护。
- 结论：TodoList 是柔性行动参考；最终回答前必须通过 Todo Sync Barrier。
- 详述：[phase5_3_A_Dynamic_TodoList总结.md](5/phase5_3_A_Dynamic_TodoList总结.md)

### ✓ 回补 A.1：Runtime Mode Router

- 状态：完成
- 目标：每个 Run 按 Conversation 选择 `plain/todo`，不固定整个 Session。
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
- 结论：只有显式 `delete_session` 才整体清理；Run/Session 结束与 Level 2 裁剪不自动删除 Artifact。
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

## Phase 6 · Goal、能力加载与委派

面向更大目标组织任务、渐进加载能力，并支持 SubAgent 委派。

### 小节索引

### 6A Goal Loop

- 状态：架构重做与自动化回归完成；新真实模型 benchmark 待补。
- 目标：围绕最终目标自动循环完整 Agent Turn，并由独立 Judge 依据冻结 Contract 和真实证据决定完成、继续或暂停。
- 当前边界：Goal 只保存 Objective、Completion Contract、Turn/Judgment 历史和生命周期；Plan/Todo 不进入 Goal 聚合。Contract 自动推导，Executor 不可修改，Judge 只能在 Turn 边界修订 inferred 标准。
- 交互入口：`/goal <objective>` 自动执行 Contract Compilation → Executor → Judge → continuation，直到 completed、paused 或耗尽 `max_goal_turns`。
- 详述：[phase_6A学习.md](6/phase_6A学习.md)

### 6B Skill / Toolset Progressive Loading + MCP Toolset Adapter

- 状态：进行中；Toolset 渐进加载的 Core 最小原型与自动化回归完成，真实 Plugin、Skill 和 MCP Adapter 待接入
- 目标：按需渐进加载 Skill / Toolset，避免一次性暴露全部能力；接入 MCP，验证外部 Toolset 的发现、选择与调用。
- 前置边界：区分 Plugin 装配、交互命令激活与单次 Run Capability 注入；6B 只处理 Run 期 Capability 的选择、加载和释放。
- Run 约束：RunInvocation 可覆盖当前 RuntimeMode；受限规划能力使用 PlainMode，避免渐进加载的工具集被 Todo 等模式再次扩张。
- MCP 定位：MCP 是外部 Toolset 的接入协议，不负责能力加载策略；具体适配放在可选 Plugin，Core 不依赖 MCP。
- 当前结论：Core 管理单次 Run 的加载状态与逐轮工具装配，Plugin 通过 `ToolsetProvider` 提供精简目录和具体工具；模型调用 `load_toolset` 后，工具从下一轮可见，Run 结束后自然释放。
- ToolSpec 前置回补：以 `ToolParameters` 绑定模型 Schema 与运行时校验；支持 Pydantic 与原生 JSON Schema，非法 Schema 和重复工具名直接失败。
- 详述：[phase_6B学习.md](6/phase_6B学习.md)；[ToolSpec格式回补总结.md](6/ToolSpec格式回补总结.md)

### 6C SubAgent Delegation

- 状态：未开始
- 目标：把子任务委派给 SubAgent 并回收结果。
- 结论：（待写）
- 详述：总结待写

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

多 Agent 协作：协调、分工、委派与共享状态。

### 小节索引

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

### Delegation

- 状态：未开始
- 目标：定义任务委派与结果回收契约。
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
