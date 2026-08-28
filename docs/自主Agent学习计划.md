# 自主 Agent 学习计划

当前实现以 [架构总览](架构/总览.md) 为准。下一轮从 [计划](计划.md) 第 2 章起。

涉及架构边界或抽象调整时，先读 [项目架构方向](项目架构方向.md)。

## Rule 同步区

- 学习交互是讨论，不是连环提问：先给判断和依据，再一起推敲。
- 接入成熟外部协议或生态前，先查官方文档和主流维护库，明确现成能力、协议陷阱与采用/不采用理由；默认只写本项目特有的窄适配，不自行重造传输、重试、解析、路由等基础设施。
- 接入 Channel 时必须分别定义 Access、Conversation、Delivery、Reply route 四种 identity。凭证不作 identity；进程重启不新建对话，更换服务账号不复用旧 Session；所有输入按 delivery 顺序成为 UserMessage Event 并唤醒 Session。见 [Channel 接入契约](架构/Channel接入契约.md)。
- 保持简单、高内聚、低耦合；只为已经出现的真实需求增加抽象。
- Runtime 内核是 `helperme/runtime`：Event 持久、State 归约、Step 决策、Command 副作用。LLM 在 `helperme/llm`，执行环境边界在 `helperme/sandbox/`，工具契约在 `helperme/tools/`；它们都不进 Runtime。
- 内部相信契约：内部契约违规与未预期异常必须原样暴露；只在 CLI、LLM、MCP、文件系统等外部输入边界捕获已知、预期且能够处理的错误。禁止用宽泛异常捕获把 bug 降级成业务失败、能力不可用或继续运行。
- 源码、Agent 状态、用户任务数据分离，并由各自的生命周期管理。
- 完成结论必须基于可验证证据，不能只依赖 Agent 自述。
- 能力执行目录与管理目录分离：disabled 能力不得进入可执行目录，但必须可被观察、诊断和提出恢复方案。
- 工具失败只证明本次动作失败。可恢复错误应给出结构化状态和下一动作；恢复后必须重新验证原目标。持久信任变更继续经过人审批。
- Runtime 不做语义判断：Host 只把明确的 yes/no 写成 `CommandAuthorized` / `CommandRejected`；其他话一律 `UserMessage`。副作用安全边界是授权，不是模型自述。后到的 `UserMessageReceived` 只改写下一次决策起点：签发冻结早于该消息的已派发 Attempt 先终态（含模型还在想时入账、随后才派发的同一轮 Command）；旧 `decision_on_outcome` 组不再单独要 Step。没有 Interrupt Event，也不杀在途工具。见 [Runtime 状态推进模型](架构/Runtime状态推进模型.md) 第 6 节。
- MCP 与 Skill 不是同一端口。MCP 经 Assistant `load_toolset`，下一 Step 才出现工具。Skill 是两个普通工具。见 [工具与能力](架构/工具与能力.md)。
- 生产代码不保留泛化 `adapters` 包。内置工具只在 `helperme/assistant/builtin_tools.py` 装配；`helperme.runtime` 除自身子模块外不得 import 其他 `helperme` 模块，也不得 import `host` / `plugins` / `tools`；Sandbox 和 Tools 不得反向依赖 Assistant 或 Runtime。

## 当前系统

```text
python console_chat.py
  → helperme.channels.cli.console
  → helperme.assistant.runner
  → SessionScheduler.wake()    每次激活至多一个 Step；不同 Session 独立并行
  → Dispatcher                 Command
  → SqliteJournal              Event
```

分层与文件见 [架构总览](架构/总览.md)。MCP 与 Web（经 MCP）现网可用。

## 判定遗留

- inferred 的自然语言编译。现在是「写过文件 / 跑过命令」的模板。
- 换目标拿不准时主动问一句。现在靠关键词，其余一律当继续当前任务。
- 独立 Judge 的真实模型触发暂不接通；与 Kanban 一起进入长任务专题，不单独补做。

`get_changes` 已完成；Level 2 见 [计划](计划.md) 第 2 章。

## 计划

从第 2 章起，见 [计划](计划.md)。当前不接通第 3 章 Judge，也不预建第 3～6 章的状态机。
