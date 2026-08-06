学习计划顺序文档。

每章节 初次只做核心原型，在后续章节做的时候，发现需要继续补充前边的章节的技术的时候，就进行回顾补充。
如果新 Phase 暴露出旧 Phase 的不足，就回补旧模块。但回补只服务当前 Phase，不做大而全重构。
做每一节任务也是对前面设计的优化过程，必须先处理进行该章节，但未满足的前置模块补充/优化。

## Rule 同步区

- 禁止为了跑通当前局部模块而添加静默兜底、隐式默认值或自动生成关键关联数据。兜底不得掩盖上层调用错误，否则会破坏整体设计并显著增加调试成本。

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
Phase 5 Context Management
│
├─ ✓ 5.1 Context Projection
├─ ✓ 5.2 Context Budget
├─ ✓ 5.2.1 Tool Result Budget / Runtime Artifact
├─ ✓ 5.3 Safe Compression
├─ ✓ 回补 A / A.1 / B / C
├─ ✓ 5.5 Workspace Sandbox
├─ ✓ 5.6 Workspace Retrieval（工具型）
└─ ◇ 5.7 Command Execution
        ↓
Phase 6 Goal、能力加载与委派
        ↓
Phase 7 Scheduler / Watcher / Background Task
        ↓
Phase 8 Multi-Agent
        ↓
Memory（后置，外挂）
```

详细执行过程按 Phase 编号见 [`docs/0/`](0/) … [`docs/5/`](5/)。

---

## Phase 0 · Agent Core

目标是做一个能够调用工具，并且能够读写文件的 agent 最小 MCP。

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

---

## Phase 2 · TodoList

让 agent 在长任务前形成可审阅 TodoList，并在执行中通过 `rewrite_todos` 自主维护。

### 小节索引

### ✓ TodoList

- 状态：完成（遗留：只读约束跟随、长任务稳定性）
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
◇ Phase 5.7 Command Execution
        ↓
Phase 6A Goal / Task Management
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

- 状态：完成验收
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

### ◇ 5.7 Command Execution（Benchmark 待验收）

- 状态：第一版实现与行为测试已完成；Agent Benchmark 待验收
- 目标：让 Agent 在指定 Workspace 中调用本机 CLI，完成依赖安装、构建、测试、Git 和包管理等真实工程任务。
- 结论：工具适配、PowerShell Runner 与有界捕获分层；Workspace 只约束 cwd，子进程环境显式构造；命令显式声明 `read_only|may_write`，默认保守按 `may_write` 处理并由 StopGuard 要求 get_changes 验证。
- 详述：[phase5_7_Command_Execution计划.md](5/phase5_7_Command_Execution计划.md)；[phase5_7_Command_Execution总结.md](5/phase5_7_Command_Execution总结.md)

---

## Phase 6 · Goal、能力加载与委派

面向更大目标组织任务、渐进加载能力，并支持 SubAgent 委派。

### 小节索引

### 6A Goal / Task Management

- 状态：未开始
- 目标：管理跨步骤的目标与任务组织。
- 结论：（待写）
- 详述：总结待写

### 6B Skill / Toolset Progressive Loading

- 状态：未开始
- 目标：按需渐进加载 Skill / Toolset，避免一次性暴露全部能力。
- 结论：（待写）
- 详述：总结待写

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
