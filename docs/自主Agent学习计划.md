# 自主 Agent 学习计划

当前实现以 [架构总览](架构/总览.md) 为准。下一轮从 [计划](计划.md) 第 2 章起。

涉及架构边界或抽象调整时，先读 [项目架构方向](项目架构方向.md)。

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

从第 2 章起，见 [计划](计划.md)。当前不接通第 3 章 Judge，也不预建第 3～5 章的状态机。
