# Phase 6D · Skill Progressive Loading

> 状态：第一版实现与真实模型 Benchmark 完成；运行时边界已在架构复盘中修订（2026-08-20）。
> 实现结论与验收证据见[Phase 6D 实现总结](phase_6D实现总结.md)。

## 目标

让 HelperMe 能安装和管理持久 Skill，并在单次 Turn 中按需加载其主指令与 supporting files，避免在 Turn 开始时注入全部 Skill 正文。

```text
Skill Plugin 中已安装并启用的 Skill
    ↓
每个 Turn 生成普通 load_skill ToolSpec，工具描述携带精简目录
    ↓
模型单独调用 load_skill(name)
    ↓
完整主指令作为普通 tool result 进入 Conversation
    ↓
模型自行决定沿用、重新加载、读取 reference 或调用普通工具执行 script
```

第一版同时完成安装、检查更新、更新、启停与卸载闭环。更新只能由用户显式操作或用户重新部署 HelperMe 触发，严禁后台或启动时静默更新。

## 核心定义

> Skill 是可发现、可复用、按需读取的详细指令包，不是一种新的执行能力。

Skill 可以包含指令、知识、工作流和 supporting files，但真正执行动作的仍是模型、普通 Tool、MCP 或代码执行器。与用户直接输入一段详细操作规程相比，Skill 新增的是可发现、可复用、可版本化和可治理的工程属性，而不是新的 Agent 执行语义。

```text
MCP / Tool
→ 系统能够执行什么外部动作

Skill
→ 面对某类任务时，模型应当如何组织已有动作
```

`load_skill` 也不负责选择或执行 Skill。模型根据 Catalog 选择稳定 ID；`load_skill(id)` 只确定性读取对应详细指令，并以普通 tool result 返回。

运行时边界遵循：

> 安全与因果约束由 Runtime 保证；工作流选择权交还模型。

因此 Runtime 只强制通用工具协议、`exclusive_batch`、参数校验和资源路径沙箱，不强制模型何时加载、是否重新加载、是否先读主指令或使用哪个 revision。Core 不保存 Skill 领域状态，不理解 Skill 的安装、版本和加载生命周期。

## Workspace 边界

```text
Task Workspace
→ execution root
→ cwd、用户输入、任务产物、Git 变化与 Evidence

Skill Plugin storage
→ resource root
→ 已安装 Skill 的 SKILL.md、references、scripts、templates、assets
```

相对路径默认属于 Task Workspace，不因加载或执行 Skill 而改变。HelperMe 不做 `chdir(skill_dir)`；Skill 脚本必须通过显式 `<skill-dir>` 路径执行。

第一版目录仍可位于 Agent Workspace 根目录之下，但路径由 Skill Plugin 自己派生和管理，不在 `AgentWorkspace` 上增加 `skills_root` 领域接口：

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

Task Workspace 的 `read_file` 不得读取 Skill 包。Skill 内容通过专属 `read_skill_resource` 访问；该工具的特殊性仅是 Skill 目录路径沙箱，不代表一套新的执行流程。脚本执行复用 `execute_command`，其 cwd 仍是 Task Workspace，输出继续进入既有有界捕获、Artifact 与 Evidence 链路。

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

Skill Runtime Tools（Plugin 内部）
→ 从 Registry 投影 enabled Descriptor 目录
→ 生成普通 load_skill / read_skill_resource ToolSpec
→ 按稳定 ID 读取主指令和 supporting files
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

安装、更新和启停属于 Application 控制面，不进入两个 Runtime ToolSpec 或 TurnRuntime。

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

第一版不实现活动 Turn 内热更新、多版本存储和旧版本 GC。手动 update 是显式维护操作：不得在活动 Turn 中替换包；提交后由下一个 Turn 重新生成 Catalog 和工具闭包，无需强制创建新 Session。

刷新的是发现目录，不会覆盖已经作为 tool result 进入 Conversation 的 Skill 指令。`load_skill` 返回 `skill_id + revision + content`，使加载时版本成为对话证据。后续 Turn 是否沿用旧内容、重新加载或请求最新版，由模型结合用户语义决定，Runtime 不维护这项工作流策略。

## 运行时对象

| 对象 | 所属边界 | 职责 |
| --- | --- | --- |
| `SkillBundle` | Plugin 管理面 | 规范化一个完整候选包 |
| `SkillRecord` | Plugin 持久层 | source、resolved ref、enabled、revision、hash 与时间戳 |
| `SkillDescriptor` | Plugin Registry 投影 | 仅含稳定 name、description 与 revision，帮助模型选择 |
| `load_skill` ToolSpec | Plugin 执行面 | 携带当前 Catalog，按 ID 返回完整主指令和 revision |
| `read_skill_resource` ToolSpec | Plugin 执行面 | 在指定 Skill 目录沙箱内读取 supporting file |

Runtime 不再需要 `LoadedSkill`、`SkillLoadingState`、`SkillProvider`、`SessionSkillSnapshot`、`SnapshotSkillProvider` 或 `TurnSkillEnvironment`。Skill Plugin 实现现有通用 `TurnCapability`，在 `tool_specs()` 被每个 Turn 调用时根据当前 Registry 生成两个普通 `ToolSpec`；Core 不增加 Skill 专属接口。Application 级 `additional_tool_specs` 在创建时冻结，只继续承载静态管理工具，不用于动态 Catalog。

## Turn 可见性与对话版本证据

每个 Turn 使用创建工具闭包时的 enabled Descriptor 集合。活动 Turn 中禁止安装控制面替换包；下一个 Turn 自动获得最新 Catalog。

已经调用过的 `load_skill` 结果属于 Conversation 历史，而不是 Runtime 私有状态：

```text
Turn A：load_skill(pdf) → revision=abc + 完整正文 → 提出方案
Turn B：用户确认 → 模型可以直接依据 revision=abc 继续执行
```

如果 Turn A 只口头选择 Skill 而没有加载，Turn B 再加载；如果内容因上下文处理不可用、用户要求最新版或模型判断任务已变化，模型可以重新加载。Runtime 不为这些情形编写强制状态机。

## 渐进发现与加载

### 第一层：精简目录

第一版在每个 Turn 生成 `load_skill` ToolSpec 时，将全部 enabled Skill 的 `name + description` 放入工具描述或参数 Schema：

```text
- python-testing: 指导 Python 项目的测试设计与执行
- pdf: 创建、读取和验证 PDF
```

revision、source、hash 和安装信息不进入 Catalog。目录顺序固定，保证相同 Registry 投影产生稳定 ToolSpec。

暂不实现分类、搜索或 `list_skills`。优化边界必须保留：

- `SkillRegistry` 独立提供 Descriptor 投影；
- ToolSpec 工厂集中负责目录投影；
- `load_skill(name)` 只依赖稳定 ID，不依赖发现方式；
- Catalog 在 Plugin 内有明确大小上限，不能静默截断；
- `/skill enable` 检查发布后的完整目录仍在预算内。

真实目录规模触发预算问题后，再从分类、分页、关键词搜索或语义检索中选择，不提前实现。

### 第二层：完整主指令

`load_skill(name)` 成功后直接返回 `skill_id + revision + skill_dir + content`。完整正文作为普通 tool result 进入 Conversation，不写入 Runtime 私有状态，也不提升为 system/developer/runtime instructions。

```text
skill_id: python-testing
revision: <content revision>
skill_dir: <absolute skill-dir>
content: <SKILL.md 去除 Frontmatter 后的完整正文>
```

Conversation 中的工具结果就是加载证据。Goal 跨多个 Turn 时，模型可以沿用已存在的正文，也可以自行决定重新加载。重复加载不需要 Runtime 幂等状态；它只是再次读取当前 Registry 指向的内容。

### 第三层：supporting files

模型根据任务或正文按需调用：

```text
read_skill_resource(skill_id, relative_path, range)
```

只允许读取当前 enabled Skill 的包内资源，路径必须相对对应 skill_dir，不能跨 Skill。是否先加载主指令由模型决定，不由 Runtime 强制。大型 reference 使用分页或范围读取；supporting files 作为普通 tool result 进入 Conversation。

读取错误边界：

- 未知或未启用 Skill ID、非法相对路径或不存在资源：模型可修正的工具错误；
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

Skill 正文以普通 tool result 进入 Conversation，其权限不高于用户消息，更不能被提升为 system/developer/runtime instructions。第一版不建设复杂的指令冲突检测器；极端恶意或冲突内容归入外源信息注入专题。

## 预算契约

Skill Plugin 在安装、启用和 ToolSpec 生成阶段维护包大小、单个主指令和 Catalog 的确定性上限。`load_skill` 返回完整正文，不在 Skill 层建立“当前 Turn 累计已加载正文”状态。

正文进入 Conversation 后，统一服从现有工具结果与上下文预算规则。超过单条工具结果上限时，完整结果由通用 `ToolResultExternalizer` 保存为 Runtime Artifact，模型按提示使用 `read_artifact` 分页读取；不为 Skill 在 Core 建立专属预算和 Safe Compression 旁路。

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
3. `SkillRegistry`、Plugin 私有 storage root 与本地原子安装；
4. Local/GitHub/URL `SkillSource`、staging 和冻结 candidate；
5. `/skill` list/install/inspect/test/enable/disable/remove/check-update/update；
6. 更新 manifest diff、独立模型概括与 candidate hash 应用；
7. 从 Registry 投影 `SkillDescriptor`，生成携带 Catalog 的普通 `load_skill` ToolSpec；
8. `load_skill` 以普通 tool result 返回完整正文、revision 与 skill_dir；
9. `read_skill_resource` 路径沙箱与 `<skill-dir>` 脚本执行闭环；
10. 普通对话 Proposal/Approval 接入；
11. 自动化回归与真实模型 benchmark。

先完成通用独占批次和本地安装/加载闭环，再逐步增加远端来源与更新说明；每一步都保持可运行，不为后续步骤提前创建统一 Plugin 基类或通用包管理框架。

## 验收标准

### Core 行为测试

- AgentStep 1 的 `load_skill` ToolSpec 携带精简目录，没有未选择 Skill 的正文；
- `load_skill` 通过普通 tool result 返回完整正文、revision 与 skill_dir；
- Core 不存在 Skill 专属 Provider、Snapshot、LoadingState、Environment 或预算；
- Goal 的后续 Turn 可以依据 Conversation 中已有加载结果继续执行；
- 未知 ID、未启用资源、非法路径和大小超限返回可修正错误；
- `read_skill_resource` 不要求 Runtime 先记录“已加载”，但必须阻止路径越界；
- 同一 AgentStep 多个 `load_skill` 或混合批次整批不执行；
- 是否加载多个 Skill、重新加载或沿用旧 revision 由模型决定；
- Skill 正文只作为普通 tool result 进入 Conversation，不提升指令权限；
- Task Workspace 相对路径语义不因 Skill 改变。

### 安装与更新测试

- 非法 Frontmatter、身份不一致、路径穿越、symlink/junction 和越界目标被拒绝；
- staging 失败不产生 Registry 记录或半安装目录；
- 安装默认 disabled，enable 后从下一个 Turn 进入 Catalog；
- 未登记孤立目录不进入 Runtime；
- check-update 冻结 candidate hash 并产生确定性 manifest diff；
- update 只能应用已检查的确切 hash，不能重新获取漂移内容；
- 同源 update 与换源 replace 在记录和说明中明确区分；
- 更新报告优先提供语义概括，同时保留文件级证据；
- 没有用户显式 update 或重新部署时，Skill 内容绝不变化；
- 活动 Turn 中拒绝替换包，更新后由下一个 Turn 使用新 Catalog。

### 真实模型 Benchmark

准备一个包含主指令、reference、script 和模板的真实 Skill：

1. 从远端来源安装为 disabled；
2. inspect/test 后显式 enable；
3. 新 Turn 只根据 `load_skill` ToolSpec 中的精简目录选择该 Skill；
4. 模型单独调用 `load_skill`；
5. 下一 AgentStep 从 tool result 获得完整正文，按需读取 reference；
6. 以 Task Workspace 为 cwd 执行 Skill script 并生成任务产物；
7. 通过真实 Evidence 验证结果；
8. 用户在后续 Turn 确认时，模型能依据 Conversation 中已加载的 revision 继续执行；
9. 手动检查新版，展示模型概括和机器 diff；
10. 只应用冻结 candidate，并验证不存在自动更新。

完成结论必须来自 Conversation、TurnEvidence、Workspace 结果、Registry/hash 和更新 diff，不能只依赖 Agent 自述。

## 待单独展开的专题

### 外源信息注入与 Skill 信任

研究第三方 Skill 的说明、Frontmatter、主指令、references 和 scripts 进入模型与执行链后的风险，包括 Prompt Injection、指令优先级、敏感权限、来源信任、风险展示和不可绕过的阻断条件。

在专题完成前，产品语义必须明确：用户显式安装并启用的 Skill 被视为用户选择的能力；HelperMe 第一版只承诺确定性的结构、路径、大小、hash 和变更证据，不宣称能够证明 Skill 内容安全。
