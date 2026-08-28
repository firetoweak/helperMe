# HelperMe 文档

当前系统按 **Session / Event / State / Step / Command** 运行。

## 先读

| 文档 | 用途 |
|---|---|
| [自主 Agent 学习计划](自主Agent学习计划.md) | 行动依据，含 Rule 同步区 |
| [计划](计划.md) | 从第 2 章起：Level 2、长任务（Kanban + Audit）、Automation、SubAgent、Memory |
| [项目架构方向](项目架构方向.md) | 长期原则与窄端口，不是逐条军令 |
| [架构总览](架构/总览.md) | 当前分层、目录与边界 |

## 架构

| 文档                                             | 内容                               |
| ---------------------------------------------- | -------------------------------- |
| [Runtime](架构/Runtime.md)                       | Session、Journal、Step、Command、终态  |
| [Runtime 状态推进模型](架构/Runtime状态推进模型.md)          | 完整架构决策                           |
| [Assistant 与 Sandbox](架构/Assistant与Sandbox.md) | 产品装配、执行环境与工具边界                   |
| [判定](架构/判定.md)                                 | 未开始                              |
| [上下文](架构/上下文.md)                               | Journal 投影、保护窗、Artifact、预算       |
| [工具与能力](架构/工具与能力.md)                           | 环境工具、MCP Toolset、Skill           |
| [入口与授权](架构/入口与授权.md)                           | 控制台、yes/no、控制面                   |
| [Channel 接入契约](架构/Channel接入契约.md)              | 会话 identity、投递幂等、Event wake、输出路由 |
