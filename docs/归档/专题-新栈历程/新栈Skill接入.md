# 新栈 Skill 接入

> 状态：完成（2026-08-22）  
> 位置：Host / adapter；复用 `plugins/skills`  
> 旧课：[Phase 6D](../6/phase_6D学习.md)

## 做了什么

Skill 仍是可发现、按需读取的指令包，不是新的执行能力。新栈 Host 把它投影成两个普通工具：

```text
已启用 Skill 目录写进 load_skill 的工具描述
  → load_skill(id) 返回完整主指令（普通 tool result）
  → read_skill_resource 读包内文本
  → 真正做事仍是模型 + 普通工具 / MCP / execute_command
```

目录在每次决策时刷新：`/skill enable` 后下一 Step 就能看见，不必新开 Turn。没有已启用 Skill 时，这两个工具不会出现在当前 `tools` 列表里。

对照入口接上 `/skill` 控制面。对话安装仍走 Host `/skill`，不经过 Turn BLOCKED；Runtime Command 授权见 [新栈主入口与 Command 授权](新栈主入口与Command授权.md)。

## 没有做

- 把 Skill 做成 Toolset 或新 Runtime 语义
- 在 Runtime 内核恢复 `exclusive_batch`（描述里仍要求单独调用 `load_skill`）
- 让 `read_file` 能读 Skill 包
