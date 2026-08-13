# helperMe

`helperMe` 是一个用于学习和拆解自主智能体（Agent）的个人项目。项目不追求快速堆叠功能，而是沿着清晰的阶段逐步建立：模型调用、工具执行、任务规划、长会话、上下文管理，以及后续的能力加载与多 Agent 协作。


## 架构总览

当前系统按“入口 → 应用层 → Session → Run → 基础能力”组织：

```text
console_chat.py
    ↓
AgentApplication                 应用用例：create / start / resume / interrupt / delete
    ├─ Approval                  冻结控制面操作并以 yes/no 解析用户授权
    ↑ RunHost                    可选 Plugin 发起 Run 的通用窄端口
    └── plugins/goal             可选 Goal Loop / 独立 Judge 工作流
    ↓
SessionRuntime                   管理跨 Run 的 Session 状态与 Conversation
    ↓
RunRuntime                       驱动单次 Agent tool-calling loop
    ├─ RuntimeMode               为当前 Run 选择 plain / todo 执行模式
    ├─ ContextPreparationService 构造、预算和压缩模型上下文
    ├─ ModelCallService          调用模型并记录真实 token usage
    ├─ ToolsExecutor             校验并执行工具调用
    └─ ToolResultExternalizer    将过大的工具结果外置为 Artifact
```

Core 依赖在 `core/composition.py` 中组装；可选能力在各自 Plugin 的 Composition Root 中按需装配。Core 不引用具体 Plugin。

## 目录结构

```text
helperMe/
├─ console_chat.py              # 当前可运行的命令行交互入口
├─ main.py                      # 预留的 FastAPI 入口，目前尚未实现
├─ model_config.example.yaml    # 模型与工作区配置示例
├─ requirements.txt             # Python 依赖
├─ core/                        # Agent 核心领域与运行时
│  ├─ agent_application.py      # 面向 Console/API 的无状态应用服务
│  ├─ run_host.py               # 可选 Plugin 发起 Run 的公共端口
│  ├─ composition.py            # Composition Root，创建并连接所有具体组件
│  ├─ messages.py               # 完整 Conversation 事实轨迹
│  ├─ prompt.py                 # Agent 系统提示词
│  ├─ tool_registry.py          # ToolSpec、工具注册表及 OpenAI schema 导出
│  ├─ observability.py          # Run trace 的构造与日志写入
│  ├─ model_call/               # 模型配置、客户端、调用服务与响应类型
│  ├─ session/                  # Session 状态机、Run 记录、中断与恢复
│  ├─ tools_runtime/            # 单次 Run 的工具调用循环与安全停止规则
│  ├─ runtime_modes/            # plain/todo 模式协议、实现和动态路由
│  ├─ todos/                    # TodoList、rewrite_todos 与退出屏障
│  ├─ context/                  # 上下文投影、预算、压缩、摘要与状态
│  └─ runtime_artifacts/        # 大型工具结果的外置、分页读取和生命周期
├─ plugins/
│  ├─ goal/                     # 可选 Goal/Task 领域、Capability 与装配入口
│  └─ mcp/                      # MCP Registry、Client、Toolset 与对话安装
├─ tools/                       # Agent 可调用的具体工具适配器
│  ├─ workspace.py              # 多根工作区及路径沙箱
│  ├─ file_read.py              # glob、grep、read_file 等只读工具
│  ├─ file_write.py             # apply_patch、replace_all
│  ├─ file_manage.py            # 文件创建与管理
│  ├─ get_changes.py            # 写入后的变更验证
│  ├─ command_execution.py      # 命令执行工具的 Agent 接口
│  ├─ powershell_runner.py      # PowerShell 子进程、环境和有界输出捕获
│  ├─ artifact_read.py          # 外置工具结果的分页回读
│  └─ demo.py                   # 无状态工具注册示例
├─ tests/
│  ├─ core/                     # 各核心模块的单元与集成测试
│  ├─ plugins/                  # 可选插件的独立测试
│  ├─ benchmarks/               # 真实模型、多轮任务和压缩策略实验
│  ├─ test_core_suite.py        # Core 测试聚合入口
│  └─ test_plugin_suite.py      # Plugin 测试聚合入口
└─ docs/                        # 按 Phase 保存的学习计划、设计决策和总结
```

## 一次请求如何流转

```text
用户输入
  → AgentApplication.start / resume
  → SessionRuntime 写入 Conversation 并创建 RunControl
  → RunRuntime 为本次 Run 选择 RuntimeMode
  → ContextPreparationService 生成有预算约束的 ModelContext
  → ModelCallService 请求模型
      ├─ 返回最终文本：检查 Todo/写入验证等退出条件
      └─ 返回 tool_calls：ToolsExecutor 执行并写回 tool 消息
                         ↓
                    进入下一轮模型调用
  → SessionRuntime 保存 RunResult 并迁移 Session 状态
```

## 运行项目

项目当前使用 Python 和 PowerShell，依赖 OpenAI 兼容的 Chat Completions 接口。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item model_config.example.yaml model_config.yaml
```

然后编辑 `model_config.yaml`：

```yaml
model:
  name: "your-model-name"
  base_url: "https://your-model-endpoint.example/v1"
  api_key: "your-api-key"

workspace:
  root: "D:\\work\\agent"
  full_access: false

runtime:
  max_rounds: 50
  max_goal_turns: 8
  model_context_limit: 200000
  input_budget_ratio: 0.9
```

启动命令行交互：

```powershell
python console_chat.py
```

普通输入继续使用 Core Run；输入 `/goal <目标>` 后，可选 Goal Plugin 会自动推导 Completion Contract，并循环执行完整 Agent Turn 与独立 Judge 验证，直到目标完成、暂停或耗尽 `max_goal_turns`。Plan/Todo 保持为单次 Turn 内的柔性认知工具。

也可以直接用自然语言要求安装 MCP。Agent 会补问缺失信息并展示冻结后的单进程 stdio 或 HTTP 配置；输入精确的 `yes` 才会由 Application 注册、测试并启用，输入 `no` 取消。成功后控制台自动创建新 Session，使最新能力配置生效。Secret 与复合 Shell 不进入该对话安装入口。

默认情况下，文件工具只能访问 `workspace.root`。如需在整次应用生命周期内开启整机文件工具访问，在 `model_config.yaml` 中显式设置：

```yaml
workspace:
  full_access: true
```

该配置对本次 Application 生命周期固定有效；Windows 已挂载磁盘会以 `drive_c`、`drive_d` 等逻辑 root 注册。它不会绕过操作系统权限，且不改变 `execute_command` 本来就能访问 Workspace 外部资源的事实。

单次 Run 最大模型轮次在同一配置文件中设置：

```yaml
runtime:
  max_rounds: 80
```

它同时作用于普通 Run、Contract 编译、Executor Turn 和 Judge Run。`max_goal_turns` 单独限制一个 Goal 自动开启的 Executor Turn 数；`model_context_limit` 与 `input_budget_ratio` 也统一由 `runtime` 配置，不再由 Python 启动参数或控制台常量设置。

运行中按 `Ctrl+C` 会请求 Agent 在安全点中断，而不是直接破坏当前工具调用。运行日志默认写入项目的 `logs/`；可通过 `HELPER_RUN_LOG_PATH` 指定其他路径，也可通过 `HELPER_MODEL_CONFIG` 指定配置文件。

## 测试

运行核心测试套件：

```powershell
python -m unittest tests.test_core_suite
```

`tests/core/` 负责可重复的单元与集成验证；`tests/benchmarks/` 用于需要真实模型或特定场景的实验，不属于默认测试套件。

对话安装 MCP 的真实 stdio 闭环：

```powershell
python -m tests.benchmarks.phase6b_mcp_install_benchmark
```

## 设计约束

- 内部代码相信已经建立的契约，异常保留原始语义并尽早暴露。
- 只在用户输入、模型响应、工具参数、文件路径等外部边界处理预期错误。
- 不用静默兜底、隐式默认值或自动生成关键关联数据掩盖调用错误。
- 新 Phase 暴露旧模块不足时，只做服务于当前目标的最小回补，避免大而全重构。

更细的设计背景与阶段性取舍，请从 [`docs/自主Agent学习计划.md`](docs/自主Agent学习计划.md) 进入对应 Phase 文档。
