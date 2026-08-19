## Phase 5 · Context Management 总结

> 历史快照：本文保留 Phase 5 完成时的能力与验收记录。其 WorkspaceSandbox、逻辑 root 和 `root + relative path` 描述已被后续 Environment 架构替代，不形成兼容主链。当前唯一契约见 [工作区语义与工具路径契约](../专题/工作区语义与工具路径契约.md)。

状态：完成（2026.08.07）。

Phase 5 建立了同一 Session 内的上下文管理与真实 Workspace 工作闭环。Conversation 保存完整协议事实；ContextState 保存可持续更新的最小压缩状态；ModelContext 是某一轮发送给模型的临时投影。Workspace 内容和命令副作用仍是外部事实，只有经过工具读取与验证后才进入模型判断。

### 完成范围

- 5.1 Context Projection：统一 `Conversation → ModelContext` 投影。
- 5.2 Context Budget：发送前评估完整模型请求，并用真实 usage 校准。
- 5.2.1 Tool Result Budget / Runtime Artifact：单次工具结果有界，超大正文按 Session 外置回读。
- 5.3 Safe Compression：Level 1 持续性工具脱水，Level 2 增量摘要；只更新 ContextState，不修改 Conversation。
- 回补 A / A.1：Turn-local Dynamic TodoList 与按 Turn 生效的 Runtime Mode Router。
- 回补 B / C：用户输入、工具结果边界与 Artifact 生命周期。
- 5.5 Workspace Sandbox：多根、相对路径、轻量路径权限边界。
- 5.6 Workspace Retrieval：`glob / grep / read_file` 的有界显式回取。
- 5.7 Command Execution：可信本机 Workspace 中的 PowerShell 前台命令执行、有界捕获、超时与副作用验证。

### 最终不变量

```text
Conversation = 完整事实轨迹，只追加，不因压缩删除
ContextState = Session 持有的最小派生状态
ModelContext = 单次模型调用快照
Runtime Artifact = Session 私有外部正文抽屉
Workspace = 外部事实源
TurnRuntime = 执行与安全控制者，不持有长期 Session 状态
```

压缩不能补救单条无界输入；WorkspaceSandbox 不是操作系统沙箱；调用 `get_changes` 只保证完成了验证动作，不自动保证模型正确解释结果。

### 最终验收

- 5.3 端到端：同一 Session 完成 `S1 + delta → S2`，第二次摘要不重复读取已覆盖前缀；interrupt/resume 后复用已提交摘要状态。
- 5.7 真实 Agent：自主完成项目发现、依赖安装、失败测试定位、代码修改、重测、构建、Git 核对与事实一致总结；评估器独立复核测试、构建与 Git 状态。
- 全量测试：235 项通过；1 项因当前 Windows 环境无符号链接权限跳过。

### 编号与后置能力

早期路线中的 5.4 Memory Model / Memory Extraction 已明确后置。当前没有第二个长期事实数据源，也没有必须消费 Memory 的能力；提前提炼会复制 Conversation 并制造同步责任。5.5～5.7 的编号已经进入文档和提交历史，因此保留编号，不为连续外观做无价值重排。

Phase 5 不实现长期 Memory、不可信命令隔离、后台进程、调度和多 Agent。这些是后续阶段的独立能力，不是 Phase 5 的未完成项。

### 进入 Phase 6 的接口

Phase 6 可以直接依赖以下稳定事实：

- Session 能跨 Turn 保留 Conversation 与 ContextState；
- TodoList 只属于单个 Turn；
- Application 层通过显式用例操作 Session；
- 工具能力可由 Composition Root 绑定；
- Workspace 读、写、命令和验证已经形成真实行动闭环。

因此下一步不是继续扩展 Context，而是先回答：比 Turn 活得更久、需要组织多个行动单元的 Goal 应由谁持有。
