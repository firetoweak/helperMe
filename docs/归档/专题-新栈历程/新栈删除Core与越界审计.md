# 新栈删除 Core 与越界审计

> 状态：完成（2026-08-22）  
> 依据：[Agent Runtime 状态推进模型](Agent%20Runtime状态推进模型.md)、[前六章回补方向](前六章回补方向.md)

## 决策

旧 `core/`（TurnRuntime / Session / Goal / Todo）已删除。产品主链路只剩：

```text
agent_runtime   Event / State / Step / Command
host            LLM、配置、环境沙箱、工具契约、工作区
adapters        投影、判定、Toolset 表面、控制台 Host
plugins/mcp     MCP 领域
plugins/skills  Skill 领域
tools           文件与命令工具
```

`python console_chat.py` 只走新栈。`--engine core` 与 `console_core.py` 已删除。`plugins/goal` 已删除。

## 没有搬进 Runtime 的东西

| 旧物 | 去向 |
|---|---|
| TurnRuntime / TurnHost / Session | 删除 |
| Goal Loop / max_goal_turns | 删除 |
| TodoList / rewrite_todos | 删除 |
| Conversation 作为主事实 | 删除；上下文由 Journal 投影 |
| 旧 Context / MicroCompactor | 删除；新栈用 `adapters/model_context.py` |
| LLM 客户端与配置 | `host/llm/` |
| 环境沙箱 | `host/environment.py` |
| ToolSpec / 执行器 | `host/tools/` |
| AgentWorkspace | `host/workspace.py` |
| ApprovalRequest（安装提案） | `host/approval.py`，Host 控制面 |
| Command 授权 | Runtime 只消费 `CommandAuthorized` |
| MCP | Plugin + Host `load_toolset`，下一 Step 才出现工具 |
| Skill | 两个普通工具，不是新执行循环 |

## 越界审计

强制测试：`tests/adapters/test_import_boundary.py`、`tests/adapters/test_runtime_architecture.py`。

- `agent_runtime` 不得 import `host` / `adapters` / `plugins` / `tools` / `core`
- `host` 不得 import `agent_runtime` / `adapters` / `plugins` / `core`
- adapters 除 `legacy.py` 外不得 import `tools`
- `legacy.py` 不得 import `core`；它只装配环境工具与 Plugin
- `runtime_host.py` 不得出现 `TurnRuntime` / `TurnHost` / `TodoList` / `AgentApplication`

Runtime 内核没有增加：Goal、Todo、Turn 泵、MCP 名词、Skill 执行循环、exclusive_batch 语义。

## 测试

```text
tests/agent_runtime + adapters + plugins  202（1 skipped）
tests/host                                 14（1 skipped）
tests/tools                                21
tests/live                                  2  真实调模型，通过
```

端到端：`tests/live/test_llm_live.py`、`tests/live/test_runtime_live.py`。后者走 `AgentRuntime.advance` + `drive_until_idle`，断言 Journal 出现 `UserMessageReceived` 与 `StepCommitted`，并有 `deliver`。

## 还没做

- 对话里 yes 安装 MCP / Skill（仍用 `/mcp` `/skill`）
- Toolset 加载状态写成 Journal 事实
- Level 2 摘要
- Phase 7
