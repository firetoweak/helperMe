# Phase 6D · Skill Progressive Loading

## 目标

让 HelperMe 能安装和管理持久 Skill，并在单次 Turn 中按需加载其主指令与 supporting files，避免在 Turn 开始时注入全部 Skill 正文。

```text
Agent Workspace 中已安装并启用的 Skill
    ↓
Turn 开始只注入 name + description 精简目录
    ↓
模型单独调用 load_skill(name)
    ↓
当前 Turn 冻结完整主指令
    ↓
下一 AgentStep 将主指令注入 runtime instructions
    ↓
按主指令继续读取 reference 或执行 script
    ↓
Turn 结束，SkillLoadingState 自然释放
```

第一版同时完成安装、检查更新、更新、启停与卸载闭环。更新只能由用户显式操作或用户重新部署 HelperMe 触发，严禁后台或启动时静默更新。

## 核心定义

Skill 是模型完成某类任务的方法，包含指令、知识、工作流和 supporting files；Tool 是模型可以执行的外部动作。两者变化方向不同：

| 能力 | 加载后改变什么 | Turn 内状态 |
| --- | --- | --- |
| Toolset | 下一 AgentStep 可见的工具 Schema 与 handler | `ToolsetLoadingState` |
| Skill | 下一 AgentStep 的 runtime instructions 与可访问资源 | `SkillLoadingState` |

6D 复用 Turn 生命周期、Session 能力快照、工具结果预算和现有命令执行链，不把 Skill 合并进 `ToolsetProvider`、`ToolsetLoadingState` 或工具 Schema 数据模型。

## Workspace 边界

```text
Task Workspace
→ execution root
→ cwd、用户输入、任务产物、Git 变化与 Evidence

Agent Workspace / skills
→ resource root
→ 已安装 Skill 的 SKILL.md、references、scripts、templates、assets
```

相对路径默认属于 Task Workspace，不因加载或执行 Skill 而改变。HelperMe 不做 `chdir(skill_dir)`；Skill 脚本必须通过显式 `<skill-dir>` 路径执行。

第一版目录：

```text
~/.helperme/skills/
├─ registry.json
├─ packages/
│  └─ python-testing/
│     ├─ SKILL.md
│     ├─ references/
│     ├─ scripts/
│     ├─ templates/
│     └─ assets/
└─ .staging/
```

Task Workspace 的 `read_file` 不得读取 Skill 包。Skill 内容通过专属 `read_skill_resource` 访问；脚本执行复用 `execute_command`，其 cwd 仍是 Task Workspace，输出继续进入既有有界捕获、Artifact 与 Evidence 链路。

## Skill 包契约

第一版 `SKILL.md` 只强制 `name` 和 `description`：

```yaml
---
name: python-testing
description: 指导 Python 项目的测试设计与执行
---
```

采用严格单一身份：

```text
目标目录名 = SKILL.md.name = Runtime Skill ID
```

安装源目录名称可以不同；安装器读取 `name` 后规范化目标目录。`description` 是简介的唯一语义来源，Registry 只保存安装时解析出的索引副本，不能独立编辑。正文加载时移除 Frontmatter。

`version`、`author`、`dependencies` 和 `platform` 暂不作为 Runtime 必需字段。安装 revision 与内容一致性由 Registry revision 和 content hash 管理。

安装边界必须确定性校验：

- `SKILL.md` 存在且 Frontmatter 合法；
- `name` 满足稳定 ID 规则，目标目录与 name 一致；
- 包内路径必须为相对路径，禁止 `..`、绝对路径、symlink 和 junction；
- 文件数量、单文件大小和包总大小有界；
- 安装目标必须位于 Agent Workspace 的 skills root 内。

非法外部包属于安装输入错误，给用户明确报告；安装器或 Registry 产生的不一致属于内部契约错误，保留原始异常失败。

## 安装控制面与 Runtime 分离

参考 Hermes 的 Hub/Runtime 分离和 nanobot 的严格身份约定：

```text
SkillSource
→ 获取并规范化 SkillBundle
→ .staging
→ 确定性校验
→ 原子安装
→ Registry 提交

SkillProvider
→ 只读取已登记且 enabled 的 Descriptor
→ 按稳定 ID 加载主指令和 supporting files
```

第一版真实来源包括本地目录、GitHub 与直接 URL；它们统一转换为：

```text
SkillBundle
├─ name
├─ description
├─ source
├─ resolved_ref
├─ content_hash
└─ files
```

多个真实来源已经存在，因此 `SkillSource` 是合理端口；市场索引、Profile、Plugin Skill、多个社区 Hub 和自定义 Tap 不进入第一版。

`SkillRegistry` 是 Runtime 可见能力的事实源。Agent Workspace 中未登记的孤立目录、损坏包和 `.staging` 内容不会自动成为模型能力。运行路径由稳定 ID 推导，不允许 Registry 指向 skills root 外部。

### 控制入口

- 用户显式执行 `/skill install|update|enable|disable|remove`：命令本身即授权，不重复要求 yes/no；
- 普通对话中 Agent 只能提交冻结 Proposal，用户输入精确 `yes/no` 后由 Application use case 执行；
- 模型不能直接写 Registry 或 Skill 安装目录；
- 安装完成默认 disabled，`enable` 是“发布到模型可见目录”的独立动作，不代表连接测试。

安装、更新和启停属于 Application 控制面，不进入普通 `SkillProvider` 或 TurnRuntime。

## 更新策略

### 禁止自动更新

Skill 只允许在以下两种情况下更新：

1. 用户显式选择 update；
2. 用户重新部署 HelperMe，部署内容明确包含新版 Skill。

禁止后台轮询后自动应用、启动时偷偷拉取最新版、因远端分支变化自动替换。检查更新是显式只读操作，不等于更新。

普通 update 从 Registry 记录的来源获取候选；更换来源允许，但属于 `replace` 语义，必须明确展示新来源并重新走完整校验。CLI 可以表现为 `update --source`，Application 层不能把来源变化伪装成同源升级。

### 候选冻结与更新说明

```text
/skill check-update <id>
→ 获取候选 SkillBundle
→ 冻结 resolved_ref + candidate hash
→ 计算逐文件 manifest diff
→ 独立只读模型生成语义概括
→ 优先展示概括，并附可展开的机器 diff

/skill update <id> <candidate_hash>
→ 只应用已经检查过的确切候选
```

用户界面优先展示模型概括，包括工作流变化、新增或删除的能力、使用方式变化、脚本/命令/网络/凭据要求和可能受影响的任务。概括只负责体验，不能决定是否允许安装，也不能代替以下机器证据：

- 旧/新 revision、resolved ref 与 content hash；
- 新增、修改、删除的文件；
- `SKILL.md` 是否变化；
- scripts、templates、references 和 assets 的分类变化。

概括使用独立、无工具权限的模型调用，只读取冻结 diff；候选 Skill 内容不进入当前普通 Agent Conversation。模型概括失败时必须明确报告，不能伪装成“无风险”或静默跳过机器 diff。

### 运行期稳定性

第一版不实现 Skill 热更新、多版本并存和旧版本 GC。手动 update 是显式维护操作：不得在活动 Turn 中替换包；提交后重载 Skill Runtime，并创建使用新能力集合的 Session。重新部署天然形成相同边界。

这条限制保证普通任务执行期间 Skill 包不可变，同时避免为尚未出现的需求建设跨 Session 版本保留、引用计数和回收系统。

## 运行时对象

| 对象 | 生命周期 | 职责 |
| --- | --- | --- |
| `SkillDescriptor` | Provider 目录快照 | 仅含稳定 name、description 与 revision，帮助模型选择 |
| `SkillBundle` | 获取与安装过程 | 规范化一个完整候选包 |
| `SkillRecord` | Agent Workspace 持久 | source、resolved ref、enabled、revision、hash 与时间戳 |
| `LoadedSkill` | 当前 Turn | 完整主指令、skill_dir、稳定身份与 revision |
| `SkillLoadingState` | 当前 Turn | 按加载顺序保存 `LoadedSkill`，Turn 结束自然释放 |
| `SkillProvider` | Application/Session 注入 | 提供目录、完整主指令与包内资源访问 |
| `TurnSkillEnvironment` | 当前 Turn | 组装 Skill 工具、目录与已加载主指令 |

`SkillLoadingState` 不保存完整 `SkillRecord`。source、更新时间和更新候选属于安装控制面；Turn 只消费执行所需的冻结内容。

`TurnInvocation` 增加独立的可选 `skill_provider`。`TurnSkillEnvironment` 不接管 `ToolsExecutor`；它只向现有工具环境贡献当前 AgentStep 所需的 `ToolSpec` 和 runtime instructions，工具注册、名称冲突和执行结果仍由现有通用链路负责。

## Session 能力快照

Session 创建时捕获 enabled Skill 的稳定 Descriptor 集合。Skill 使用独立的 `SessionSkillSnapshot` / `SnapshotSkillProvider`，不塞入 Toolset 的 Descriptor、加载状态或 ToolSpec 模型。

第一版的正常运行期不允许更新包；Snapshot 的职责是：

- 新安装或新启用的 Skill 不进入既有 Session；
- 新增其他 Skill 不应使旧 Session 已捕获的 Skill 失效；
- 新 Session 捕获最新 enabled 目录；
- 加载某个 Skill 时只核对该 ID 的已捕获 revision；
- 若控制面违反维护边界，导致旧 Session 已捕获的 Skill 被更新、禁用、删除或破坏，明确拒绝该 Skill 的加载与资源访问，不静默切换内容。

这复用了“Session 冻结能力可见性”的规则，但没有把 Skill 与 MCP 的数据模型或外部连接语义合并。

## 渐进发现与加载

### 第一层：精简目录

第一版在每个 Turn 开始时注入全部 enabled Skill 的 `name + description`：

```text
- python-testing: 指导 Python 项目的测试设计与执行
- pdf: 创建、读取和验证 PDF
```

revision、source、hash 和安装信息不进入 Prompt。目录顺序固定，保证相同能力快照产生稳定请求。

暂不实现分类、搜索或 `list_skills`。优化边界必须保留：

- `SkillProvider` 独立提供 Descriptor；
- `skill_catalog_instruction()` 集中负责目录投影；
- `load_skill(name)` 只依赖稳定 ID，不依赖发现方式；
- Skill Catalog 拥有独立预算，不能静默截断；
- `/skill enable` 检查发布后的完整目录仍在预算内。

真实目录规模触发预算问题后，再从分类、分页、关键词搜索或语义检索中选择，不提前实现。

### 第二层：完整主指令

`load_skill(name)` 成功后只返回加载回执，完整正文不进入 tool result 或 Conversation。正文原子写入当前 `SkillLoadingState`，从下一 AgentStep 开始通过 runtime instructions 注入。

```text
已加载 Skill：python-testing
Skill Directory：<absolute skill-dir>

<SKILL.md 去除 Frontmatter 后的完整正文>
```

Conversation 中的历史回执只说明过去发生过加载，不代表新 Turn 已加载。Goal 可以跨多个 Turn，但每个 Executor/Judge Turn 都必须根据当前任务重新选择 Skill。

同一 Turn 可以先后加载多个 Skill；每个 AgentStep 只允许加载一个。重复加载同一 Skill 幂等返回已加载回执。

### 第三层：supporting files

加载主指令后，模型根据正文路由按需调用：

```text
read_skill_resource(skill_id, relative_path, range)
```

只允许读取当前 Turn 已加载 Skill 的包内资源，路径必须相对对应 skill_dir，不能跨 Skill。大型 reference 使用分页或范围读取；主指令必须完整加载，但 supporting files 不永久注入所有后续 AgentStep。

读取错误边界：

- 未加载 Skill、未知 Skill ID、非法相对路径或不存在资源：模型可修正的工具错误；
- Registry 指向损坏包、已登记内容与 hash/Frontmatter 不一致：内部安装契约破坏，保留原始异常失败。

## Skill 脚本执行

第一版支持直接执行 Skill 自带脚本，不只实现文本读取。

```text
代码来源：<skill-dir>/scripts/...
执行 cwd：Task Workspace
输入输出：Task Workspace
结果处理：现有 execute_command / Artifact / Evidence
```

Skill 主指令通过显式 `<skill-dir>` 告诉模型资源根。第一版不引入 `skill://`：本机 CLI 不能直接执行 URI，引入解析器或专属脚本运行器没有当前收益。未来出现多个资源 Provider 或非文件型 Skill 后，再重新评估 URI namespace。

不允许“执行 Skill 时自动 chdir 到 Skill Directory”的隐式行为。脚本若需要随包资源，应通过脚本自身位置或显式 `<skill-dir>` 定位；相对输入输出仍属于任务。

外源脚本和指令的信任、Prompt Injection、权限影响与更细风险策略另立专题。该专题不阻塞第一版功能，但扫描结果不得被宣传为安全证明；路径、大小和包结构等确定性边界仍必须在第一版实现。

## 上下文与优先级

Skill 只能补充完成任务的领域方法，不能覆盖 Core 规则、Goal Contract、RuntimeMode、Todo Sync Barrier 或工具协议。

Skill 正文使用带 Skill ID 的独立边界块注入。第一版不建设复杂的指令冲突检测器；上下文组装保持明确顺序，使 Runtime/Goal 控制指令拥有最终解释权。极端恶意或冲突指令归入外源信息注入专题。

## 预算契约

Skill 主指令是执行契约，不允许截断、摘要或由 Safe Compression 删除：

```text
完整正文可装入 → 原子提交 LoadedSkill
无法完整装入   → SKILL_CONTEXT_LIMIT，状态不变
```

预算分层：

- `enable/test` 验证单个 `SKILL.md` 能完整加载；
- `load_skill` 验证当前 Turn 已加载正文加上新正文不超过 Skill 累计预算；
- 既有 `ContextBudget` 检查 Conversation、工具 Schema、运行时指令组成的完整模型请求。

Skill 层不依赖 `ContextPreparation` 精算整个请求；完整请求仍由现有预算组件负责。若新 Skill 加载失败，已经加载的 Skill 保持不变。

## 同一 AgentStep 独占加载

现有工具批次会并发执行。为保证“一个 AgentStep 只加载一个 Skill”且不由异步调度决定胜者，`load_skill` 必须在批次执行前声明独占：

```text
单独调用 load_skill
→ 正常执行

两个 load_skill
或 load_skill + 其他工具
→ 整批不执行
→ EXCLUSIVE_TOOL_REQUIRES_EXCLUSIVE_BATCH
```

不能复用 `control_boundary` 冒充该语义，因为它同时承担 ApprovalRequest 约束。Core 最小回补是在 `ToolSpec` 增加 `exclusive_batch: bool = False`，由批次执行器统一预检；控制工具也可以复用这项通用独占语义。`read_skill_resource` 是普通只读工具，可与其他工具并发。

## 第一版不做

- 后台或启动时自动更新；
- 运行期热更新、多版本并存与旧版本 GC；
- Skill 市场、多个社区 Hub、Profile 和 Plugin Skill 覆盖；
- 目录分类、分页、关键词或语义检索；
- `skill://` URI 与专属脚本执行框架；
- 自动注入全部 references、scripts、templates 或 assets；
- 复杂指令冲突检测；
- 把模型生成的更新概括当作安全结论。

外源信息注入的信任、Prompt Injection 与权限传播单独形成专题，不以不透明的“安全扫描器”阻塞 6D 主闭环。

## 实现顺序

1. `ToolSpec.exclusive_batch` 与批次预检测试；
2. Skill 包模型、Frontmatter/路径/大小校验和 `SkillBundle`；
3. `SkillRegistry`、AgentWorkspace `skills_root` 与本地原子安装；
4. Local/GitHub/URL `SkillSource`、staging 和冻结 candidate；
5. `/skill` list/install/inspect/test/enable/disable/remove/check-update/update；
6. 更新 manifest diff、独立模型概括与 candidate hash 应用；
7. `SkillDescriptor`、`SkillProvider` 与 Session Skill 快照；
8. `TurnSkillEnvironment`、`load_skill`、完整正文注入与预算；
9. `read_skill_resource` 分页读取与 `<skill-dir>` 脚本执行闭环；
10. 普通对话 Proposal/Approval 接入；
11. 自动化回归与真实模型 benchmark。

先完成通用独占批次和本地安装/加载闭环，再逐步增加远端来源与更新说明；每一步都保持可运行，不为后续步骤提前创建统一 Plugin 基类或通用包管理框架。

## 验收标准

### Core 行为测试

- AgentStep 1 只有精简目录和 `load_skill`，没有未加载正文；
- `load_skill` 只返回回执，正文从下一 AgentStep 完整进入 runtime instructions；
- 新 Turn 不继承上一 Turn 的 `SkillLoadingState`；
- Goal 的不同 Turn 必须重新加载所需 Skill；
- 未知 ID、未加载资源、非法路径和预算超限返回可修正错误；
- 主指令不截断，失败时不留下半加载状态；
- 同一 AgentStep 多个 `load_skill` 或混合批次整批不执行；
- 多个 Skill 可跨 AgentStep 按加载顺序共存；
- Skill 正文不写入 Conversation，不被 Safe Compression 摘要；
- Task Workspace 相对路径语义不因 Skill 改变。

### 安装与更新测试

- 非法 Frontmatter、身份不一致、路径穿越、symlink/junction 和越界目标被拒绝；
- staging 失败不产生 Registry 记录或半安装目录；
- 安装默认 disabled，enable 后只进入新 Session 目录；
- 未登记孤立目录不进入 Runtime；
- check-update 冻结 candidate hash 并产生确定性 manifest diff；
- update 只能应用已检查的确切 hash，不能重新获取漂移内容；
- 同源 update 与换源 replace 在记录和说明中明确区分；
- 更新报告优先提供语义概括，同时保留文件级证据；
- 没有用户显式 update 或重新部署时，Skill 内容绝不变化；
- 活动 Turn 中拒绝替换包，更新后重载能力并使用新 Session。

### 真实模型 Benchmark

准备一个包含主指令、reference、script 和模板的真实 Skill：

1. 从远端来源安装为 disabled；
2. inspect/test 后显式 enable；
3. 新 Session 只根据精简目录选择该 Skill；
4. 模型单独调用 `load_skill`；
5. 下一 AgentStep 遵循完整正文，按需读取 reference；
6. 以 Task Workspace 为 cwd 执行 Skill script 并生成任务产物；
7. 通过真实 Evidence 验证结果；
8. 新 Turn 不继承加载状态；
9. 手动检查新版，展示模型概括和机器 diff；
10. 只应用冻结 candidate，并验证不存在自动更新。

完成结论必须来自 Conversation、TurnEvidence、Workspace 结果、Registry/hash 和更新 diff，不能只依赖 Agent 自述。

## 待单独展开的专题

### 外源信息注入与 Skill 信任

研究第三方 Skill 的说明、Frontmatter、主指令、references 和 scripts 进入模型与执行链后的风险，包括 Prompt Injection、指令优先级、敏感权限、来源信任、风险展示和不可绕过的阻断条件。

在专题完成前，产品语义必须明确：用户显式安装并启用的 Skill 被视为用户选择的能力；HelperMe 第一版只承诺确定性的结构、路径、大小、hash 和变更证据，不宣称能够证明 Skill 内容安全。
