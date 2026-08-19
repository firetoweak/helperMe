# Phase 6D · Skill Progressive Loading 实现总结

## 状态

第一版实现与验收完成（2026-08-20）。

## 最终链路

```text
Local / GitHub / HTTPS URL
→ SkillSourceRouter 获取并固定 resolved ref
→ 严格校验 Frontmatter、身份、路径、链接、大小和 hash
→ .staging 二次校验
→ packages/<skill-id> 原子发布
→ Registry 提交（默认 disabled）
→ enable 后由新 Session 捕获 SkillDescriptor 快照
→ Turn 只注入 name + description 目录
→ 模型单独调用 load_skill
→ 下一 AgentStep 注入完整主指令和 <skill-dir>
→ read_skill_resource 按需分页读取 supporting files
→ execute_command 以 Task Workspace 为 cwd 执行 Skill 脚本
```

Skill 领域实现位于 `plugins/skills/`。Core 只回补了真实共享语义：`ToolSpec.exclusive_batch`、`TurnInvocation.skill_provider`、Turn Skill Environment 和 Session Skill Snapshot。Skill 没有并入 Toolset 目录、加载状态或 Schema 模型。

## 关键结论

- `control_boundary` 只表示工具可返回 `ApprovalRequest`；`exclusive_batch` 独立表示工具必须独占 AgentStep。MCP Proposal 与 `load_skill` 共用同一批次预检机制。
- 非法独占批次不执行任何 handler，但每个 tool call 仍写入 `ToolsState` 和 `TurnEvidence` 并获得结构化失败，保持模型协议闭合。
- `load_skill` 只返回回执，主指令原子写入 Turn 私有 `SkillLoadingState`，不进入 Conversation、Evidence 或 Safe Compression。
- Session 冻结 enabled Descriptor。新增其他 Skill 不会使旧 Session 失效；已捕获 Skill 被更新、停用、删除或破坏时，只拒绝该 Skill 的加载和资源读取。
- Goal Executor 与 Judge 获得 Skill Provider，但每个 Turn 重新创建加载状态，不继承上一 Turn 的已加载正文。
- Task Workspace 的文件工具不读取 Skill 包；`read_skill_resource` 只允许当前 Turn 已加载 Skill 的包内文本资源。Skill 脚本不隐式 `chdir`，相对输入输出继续属于 Task Workspace。
- 安装 Proposal 会先冻结确定包；用户批准后只安装该 hash。disabled Skill 仍通过管理目录、inspect 和 test 对 Agent 可见，启用同样使用冻结 revision/hash 审批。
- `check-update` 只读取一次来源，冻结 candidate hash，产生按 instructions/references/scripts/templates/assets 分类的机器 diff，并通过无工具模型生成语义概括。`update` 只应用指定候选；来源变更显式标记为 `replace`。
- 本地、ZIP 和安装目标都禁止路径穿越、绝对包路径、symlink 和 junction。GitHub tree URL 支持 monorepo 子目录，并在安装记录中固定为 commit URL。

## 第一版预算

- 单包最多 512 个文件；
- 单文件最多 2 MiB，总包最多 20 MiB；
- 单个主指令最多 100,000 字符；
- enabled 精简目录最多 20,000 字符；
- 单 Turn 已加载主指令累计最多 200,000 字符；
- `read_skill_resource` 单次最多 50,000 字符，默认 20,000。

这些是第一版硬边界，不由 Safe Compression 补救。

## 验收证据

- 自动化全量回归：见实现完成时的 `pytest -q` 结果。
- 真实模型 Runtime + Update Benchmark：`python -m tests.benchmarks.phase6d_skill_live_benchmark`。证据包含 `load_skill`、`read_skill_resource`、`execute_command`、`get_changes`、任务产物正文、独立更新概括、machine diff 和冻结 hash 应用。
- 真实 GitHub 远程安装：`python -m tests.benchmarks.phase6d_remote_skill_install_smoke`。验证 GitHub tree URL 解析为确定 commit、安装默认 disabled、Registry hash 与包内容一致。

## 仍保留的边界

外源 Skill 的 Prompt Injection、指令信任、敏感权限和进程级 Sandbox 仍是独立专题。第一版只承诺可验证的结构、路径、大小、hash、变更证据与用户审批，不宣称能证明外源内容安全。
