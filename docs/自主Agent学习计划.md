# 自主 Agent 学习计划

当前实现以新架构为准。旧 Phase 0～6（TurnRuntime / Session / Goal / Todo）已归档，见 [归档](归档/README.md)。

涉及架构边界或抽象调整时，先读 [项目架构方向](项目架构方向.md) 和 [架构总览](架构/总览.md)。

## Rule 同步区

- 保持简单、高内聚、低耦合；只为已经出现的真实需求增加抽象。
- Runtime 内核是 `helperme/runtime`：Event 持久、State 归约、Step 决策、Command 副作用。LLM 在 `helperme/llm`，执行环境边界在 `helperme/sandbox/`，工具契约在 `helperme/tools/`；它们都不进 Runtime。
- 内部相信契约：内部契约违规与未预期异常必须原样暴露；只在 CLI、LLM、MCP、文件系统等外部输入边界捕获已知、预期且能够处理的错误。禁止用宽泛异常捕获把 bug 降级成业务失败、能力不可用或继续运行。
- 源码、Agent 状态、用户任务数据分离，并由各自的生命周期管理。
- 完成结论必须基于可验证证据，不能只依赖 Agent 自述。
- 能力执行目录与管理目录分离：disabled 能力不得进入可执行目录，但必须可被观察、诊断和提出恢复方案。
- 工具失败只证明本次动作失败。可恢复错误应给出结构化状态和下一动作；恢复后必须重新验证原目标。持久信任变更继续经过人审批。
- 人负责目标；模型补全判定；后一句默认改 inferred；只有人明确换事才改目标。不恢复 Goal / Plain 并列，不引入 TodoList。见 [判定](架构/判定.md)。
- Runtime 不代替模型判断用户一句话的含义。Host 只把明确的 yes/no 写成 `CommandAuthorized` / `CommandRejected`；其他话一律 `UserMessage`。副作用安全边界是授权，不是模型自述。
- MCP 与 Skill 不是同一端口。MCP 经 Assistant `load_toolset`，下一 Step 才出现工具。Skill 是两个普通工具。见 [工具与能力](架构/工具与能力.md)。
- 生产代码不保留泛化 `adapters` 包。内置工具只在 `helperme/assistant/builtin_tools.py` 装配；`helperme.runtime` 除自身子模块外不得 import 其他 `helperme` 模块，也不得 import `host` / `plugins` / `tools`；Sandbox 和 Tools 不得反向依赖 Assistant 或 Runtime。
- SubAgent、后台定时任务、Long Memory 是已确认的后续方向，但当前不预建其状态机；它们分别从 Assistant 委派、Automation 外部唤醒、Memory 投影接入，不进入 Runtime Core。

## 当前系统

```text
python console_chat.py
  → helperme.channels.cli.console
  → helperme.assistant.runner
  → AgentRuntime.advance()     Step
  → Dispatcher                 Command
  → SqliteJournal              Event
```

分层与文件见 [架构总览](架构/总览.md)。

## 未做

- 对话里 yes 安装 MCP / Skill（现用 `/mcp` `/skill`）
- Toolset 加载状态写成 Journal 事实
- Level 2 摘要
- 换目标拿不准时主动问一句
- inferred 的自然语言编译（现为模板）
- Phase 7 及以后
