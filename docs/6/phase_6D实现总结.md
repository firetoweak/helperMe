# Phase 6D · Skill Progressive Loading 实现总结

## 状态

第一版实现与验收完成（2026-08-20），随后完成架构复盘与 Runtime 重构。安装管理面与 Benchmark 证据继续有效；当前实现已按[修订后的 6D 设计](phase_6D学习.md)收缩边界。

## 架构复盘结论

第一版把 Skill 当作一种具有 Session 快照、Turn 私有加载状态和动态 runtime instructions 的运行时能力。真实闭环证明这套方案能够工作，也暴露出错误抽象：Skill 本质是可发现、可复用、按需读取的详细指令包，不是新的执行引擎。

判断耦合的关键不是修改文件数量，而是 Skill 策略变化是否迫使 Core 一起变化。第一版中，将 Skill 从“Session 冻结”改为“下一个 Turn 可见”，会同时影响 Session state/runtime、TurnInvocation、ToolEnvironment、Composition 和 Workspace，说明 Skill 领域策略已经反向进入 Core。

`AgentWorkspace.skills_root` 是较早出现的设计气味：Core Workspace 开始以同级属性枚举某个 Plugin 的领域目录。随着能力增加，这会自然长成 `skills_root`、`memory_root`、`scheduler_root` 等接口集合。修订方向是保留通用 Agent Workspace 根目录，由各 Plugin 自己派生和管理私有存储路径。

### 保留

- `ToolSpec.exclusive_batch` 与统一批次预检：这是 MCP Proposal 和 `load_skill` 已共同证明的通用执行语义；
- Skill 包校验、Registry、来源、原子安装、冻结候选、更新 diff、审批与 Console 管理面；
- `load_skill` / `read_skill_resource` 两个名字和路径沙箱；
- 脚本复用 `execute_command`、Task Workspace、Artifact 与 Evidence 链路。

### 下沉到 `plugins/skills`

- Skill storage root 的派生和初始化；
- enabled Descriptor 的 Catalog 投影；
- 携带 Catalog 的 `load_skill` ToolSpec 工厂；
- `load_skill` 对 SKILL.md、revision 和 skill_dir 的确定性读取；
- `read_skill_resource` 对 enabled Skill 与包内路径的校验；
- Skill 包和 Catalog 的大小限制。

### 从 Core 删除

- `AgentWorkspace.skills_root`；
- `TurnInvocation.skill_provider` 与 `create_agent_application(default_skill_provider=...)`；
- `SessionSkillSnapshot`、`SnapshotSkillProvider` 和 Session state 中的 Skill 字段；
- `SkillLoadingState`、`LoadedSkill`、`SkillBudget`；
- ToolEnvironment 中的 Skill 专属分支；
- 将 Skill 正文重新注入 runtime instructions 的逻辑。

验证发现 Application 级 `additional_tool_specs` 在创建时冻结，不能刷新下一 Turn 的 Catalog。重构没有新增接口，而是复用现有通用 `TurnCapability.tool_specs()`；该方法在每个 Turn 创建工具环境时调用：

```text
Skill Registry
→ SkillRuntimeCapability.tool_specs()
→ Plugin 为当前 Turn 生成 load_skill / read_skill_resource ToolSpec
→ Core 按普通工具执行并把结果写回 Conversation
→ 模型自行决定沿用、重新加载、读取资源或调用其他工具
```

重构完成后，`core/` 不再包含任何 Skill 领域名词或类型引用。Goal Executor/Judge 通过保留调用方已有 `TurnCapability` 获得 Skill 工具，不再需要 Skill 专属字段或跨 Plugin 依赖。

## 第一版历史链路

以下记录 commit `34d2f24` 已实现并通过 Benchmark 的链路，用于解释重构起点，不再代表修订后的目标设计：

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

第一版 Skill 领域主体位于 `plugins/skills/`，同时向 Core 加入了 `ToolSpec.exclusive_batch`、`TurnInvocation.skill_provider`、Turn Skill Environment 和 Session Skill Snapshot。复盘后只有 `exclusive_batch` 被认定为真实共享语义，其余 Skill 专属接入均列入重构范围。

## 第一版实现事实

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

- 重构后自动化全量回归：`410 passed, 2 skipped, 101 subtests passed`。新增测试覆盖每 Turn Catalog 刷新、正文进入 Conversation、跨 Turn 沿用 revision、无需加载状态读取资源、旧工具闭包拒绝漂移，以及长正文复用通用 Runtime Artifact。
- 重构后真实模型 Runtime + Update Benchmark：`python -m tests.benchmarks.phase6d_skill_live_benchmark`。模型首次将 `load_skill + glob` 混合调用，被 `exclusive_batch` 整批拒绝后自行纠正；随后从普通 tool result 获得正文，完成 `read_skill_resource`、`execute_command`、`get_changes`、产物验证、独立更新概括、machine diff 和冻结 hash 应用，所有检查通过。
- 真实 GitHub 远程安装：`python -m tests.benchmarks.phase6d_remote_skill_install_smoke`。验证 GitHub tree URL 解析为确定 commit、安装默认 disabled、Registry hash 与包内容一致。

## 仍保留的边界

外源 Skill 的 Prompt Injection、指令信任、敏感权限和进程级 Sandbox 仍是独立专题。第一版只承诺可验证的结构、路径、大小、hash、变更证据与用户审批，不宣称能证明外源内容安全。
