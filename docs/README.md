# HelperMe 文档

当前系统按 **Session / Event / State / Step / Command** 运行。

架构按变化原因阅读。每个主题一份准绳；对照文档只在改那个边界时打开，不要从专题笔记交叉入门。

## 先读

| 文档 | 用途 |
|---|---|
| [计划](计划.md) | 学习顺序、待解决的架构问题与后置专题 |
| [项目架构方向](项目架构方向.md) | 长期原则与窄端口，不是逐条军令 |
| [架构总览](架构/总览.md) | 当前分层、目录、测试分层与文档地图 |

## 使用

| 文档 | 用途 |
|---|---|
| [模型配置](模型配置.md) | DeepSeek、OpenAI、Claude、Gemini、Azure、OpenRouter、vLLM、Ollama 可复制配置 |

## 架构

### 内核与装配

| 文档 | 角色 |
|---|---|
| [Runtime](架构/Runtime.md) | 准绳：事实、推进、持久化与恢复 |
| [Assistant 与 Sandbox](架构/Assistant与Sandbox.md) | 模块对照：产品装配到代码路径 |

### 模型看见的世界

| 文档 | 角色 |
|---|---|
| [上下文](架构/上下文.md) | 准绳：投影、保护窗、Artifact、预算 |
| [多模态附件](架构/多模态附件.md) | 工具回图与 TUI 粘贴 |
| [Compact](架构/Compact.md) | 准绳：同 Session 窗口切换 |
| [LoopGuard](架构/LoopGuard.md) | 进展审视提醒，不负责窗口切换 |
| [前缀稳定性](架构/上下文窗口与前缀稳定性.md) | SYS／工具呈现，不负责窗口切换 |
| [Self-Handoff 实施设计](架构/Self-Handoff实施设计.md) | Compact 的设计约束，不是入门 |

### 能力

| 文档 | 角色 |
|---|---|
| [工具与能力](架构/工具与能力.md) | 准绳：环境工具、MCP、Skill 三个端口 |
| [LiteLLM 接入](架构/LiteLLM接入.md) | 进程内 Router、配置所有权与协议扩展 |

### 入口与多会话

| 文档 | 角色 |
|---|---|
| [Channel 接入契约](架构/Channel接入契约.md) | 准绳：identity、投递幂等、Event wake、输出路由 |
| [入口与授权](架构/入口与授权.md) | 当前 TUI / Telegram 行为 |
| [ACP 映射](架构/ACP映射.md) | 当前 ACP v1 已接线 / 未接线清单 |
| [Channel 协议改造](架构/Channel协议改造.md) | 架构方向：TUI / Web / ACP / Satori 平级，Satori 暂缓 |
| [Web](架构/Web.md) | Web Channel、前端状态边界与首版纵向切片 |
| [多活跃会话](架构/多活跃会话.md) | 每 Session 进程、Host、进程身份与父子唤醒 |
| [SubAgent](架构/SubAgent.md) | 委派、回收、只读边界 |

### 未开始、讨论与实验

| 文档 | 角色 |
|---|---|
| [判定](架构/判定.md) | 未开始；第 3 章前不按现行架构读 |
| [进程资源共享与隔离](讨论/进程资源共享与隔离0916.md) | 2026-09-16 讨论：Worker 冷启动、LiteLLM 副本与隔离边界；不是准绳 |
| [上下文压缩实验](实验/上下文压缩0828.md) | 2026-08-28 评测笔记，不是当前实现准绳 |
