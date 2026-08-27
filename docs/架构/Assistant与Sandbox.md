# Assistant 与 Sandbox

Assistant 把产品接到 Runtime：调用模型、装配工具、投影上下文并编排判定。模型窄接口位于 `helperme/llm/`；执行环境边界位于 `helperme/sandbox/`；工具契约与执行器位于 `helperme/tools/`；MCP 领域位于 `helperme/mcp/`。控制台输入属于 CLI Channel。

Assistant 应用层负责接收外部选定的 Session identity，但不替 Channel 或 Automation 选择当前 Session。identity 交给 Runtime 后，创建、持久执行和重放属于 Core；Scheduler 依据已提交事实唤醒它。

授权策略由产品装配：`ToolSpec.requires_authorization → ToolBinding → Command`。静态环境工具和动态 Toolset 使用同一条链；Runtime 只消费冻结后的要求及 Journal 中的授权事实。

Assistant 内部相信 Runtime、Dispatcher 与 Tool Binding 的代码契约。`SessionScheduler` 不吞掉 Dispatcher 的未预期异常；CLI 也不能将其打印后继续。异常发生前已经持久化且没有 Outcome 的 Attempt 保持 `unknown`；`/resume` 不自动重试或协调，当前调用链不得把内部 bug 伪装成正常恢复流程。

具体实现约束以 [项目架构方向：代码实现原则](../项目架构方向.md#代码实现原则内部相信契约) 为准。当前 Pydantic 工具输入统一使用 strict + forbid-extra；MCP / Skill Registry、Secret Store、Runtime Event / Checkpoint 使用精确版本 schema；宽泛异常捕获只允许清理、回滚与聚合后重新抛出。各外部 Adapter 对文件系统、模型和网络等已知失败做确定转换，其他异常直接穿透 Assistant 与 Channel。

## 包

| 路径 | 内容 |
|---|---|
| `helperme/llm/api.py` | Assistant 使用的 `LLMApi` 窄协议与公共调用错误 |
| `helperme/llm/client.py` | 当前唯一 OpenAI-compatible 客户端实现 |
| `helperme/llm/config.py` / `types.py` | Provider 连接配置与调用结果类型 |
| `helperme/sandbox/api.py` | Environment 选择、绑定与 Provider 窄协议 |
| `helperme/sandbox/workspace.py` | Workspace View、权限与路径解析 |
| `helperme/sandbox/local/` | 本机 Environment Provider 与 PowerShell 进程执行 |
| `helperme/tools/spec.py` | ToolSpec、参数契约与当前 OpenAI-compatible schema 导出 |
| `helperme/tools/registry.py` | Tool 注册、选择与内建注册表 |
| `helperme/tools/executor.py` | 参数解析、校验与工具执行结果标准化 |
| `helperme/tools/control.py` | `ControlApprovalRequest/Execution`（管理控制面，不是 Runtime Command 授权） |
| `helperme/tools/builtin/` | 文件、变更检查与命令执行工具实现 |
| `helperme/mcp/` | MCP Registry、Client、控制面与 Provider 侧 Toolset 类型 |
| `helperme/assistant/decision.py` | 模型响应到 `ModelDecision`、冻结上下文与 Replay Manifest |
| `helperme/assistant/context/` | Decision Context 投影、输入预算与 Assistant Prompt |
| `helperme/assistant/artifacts.py` | 按 Session 隔离的 Artifact Store 与 `read_artifact` Binding |
| `helperme/assistant/delivery.py` | 将模型正文转换为可靠投递 Command，并装配 `deliver` Binding |
| `helperme/assistant/completion/` | 完成标准、独立 Judge 与专用 Prompt |
| `helperme/assistant/runner.py` | Event wake、跨 Session 并行激活、单 Session single-flight |
| `helperme/assistant/sessions.py` | 给定 identity 后的 Session 应用操作与 Channel View |
| `helperme/assistant/assembly.py` | Assistant 能力装配 |
| `helperme/assistant/builtin_tools.py` | 当前 Sandbox/环境与内置工具装配 |
| `helperme/assistant/mcp.py` | MCP Toolset 到 Assistant Toolset 端口的翻译 |
| `helperme/assistant/control.py` | 对话控制提案的提交后执行、待确认状态与 Application 审批分派 |
| `helperme/assistant/management.py` | MCP / Skill 管理域目录、渐进激活投影，以及诊断 ToolSpec 到普通 Runtime Binding 的窄适配 |
| `helperme/assistant/skills.py` | Skill 到两个普通 Runtime 工具的翻译 |
| `helperme/assistant/toolsets.py` | Toolset 目录端口、渐进加载、激活投影与缓存恢复 |
| `helperme/config.py` / `paths.py` | 完整应用配置与 `HelperMeHome` 产品数据布局（MCP / Skills 独立根） |
| `helperme/bootstrap.py` | Journal、Runtime、Assistant 与外部资源生命周期装配 |
| `helperme/channels/cli/console.py` | CLI 循环、并发输入与有序 `UserMessageReceived` 映射 |

Sandbox 不 import Assistant、Runtime 或 Tools；Runtime 也不 import Sandbox。Tools 只消费 Sandbox 的窄契约。

## 决策器

`JournalBackedLlmDecisionMaker` 位于 `helperme/assistant/decision.py`。它从 Journal 快照投影消息，带上当前 Step 可见的 tools，调用 `helperme.llm`，把结果译成 `ModelDecision`。content-only 由 Assistant 补 `deliver`。

每条 `UserMessageReceived` 都可以成为后续 DecisionMaker 的 trigger。模型调用期间到达的新消息不进入当前冻结 Context，也不取消当前 Step；Scheduler 按 Journal 顺序继续推进。

## 环境

任务文件在配置的 workspace root。`full_access` 时再挂上 Host 根。命令走 `sandbox/local/powershell.py`。路径契约在 `sandbox/workspace.py`：Agent 不拥有工作目录，Environment 描述在哪里执行。`HelperMeHome` 只表示产品自身数据目录，不能充当任务 Workspace 或 Sandbox。

## 配置

`~/.helperme/config.json`：模型、Workspace、Runtime 与 Channel 配置的统一用户入口。首次启动缺少默认配置时，Host 创建带占位值的初始 JSON，提示用户编辑后结束本次启动。配置只在启动边界严格解析为各领域的内部类型，消费者不直接读取 JSON。Runtime 配置只包含 model_context_limit 与 input_budget_ratio，不读取 Step 次数预算。Assistant Scheduler 每次激活最多执行一个 Step，仍为 `RUNNABLE` 时再次激活；不同 Session 不经过全局执行队列，默认并行。
