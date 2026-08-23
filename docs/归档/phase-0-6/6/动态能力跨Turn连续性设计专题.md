# 动态能力跨 Turn 连续性设计专题

## 专题背景

Phase 6B 的真实 MCP 测试出现了以下连续对话：

```text
Turn 1
用户：mcp-everything 是干嘛用的？
Agent：load_toolset("mcp:mcp-everything")
Runtime：发现工具并把 ToolSpec 注册到当前 Turn

Turn 2
用户：你能用它做什么有趣的事情？
Agent：根据历史回答构造裸工具名
Runtime：TOOL_NOT_FOUND
Agent：错误推断 MCP 尚未安装
```

MCP 安装、Session 能力快照、连接和 `tools/list` 实际均已成功。失败发生在动态能力跨 Turn 延续时，因此这不是 MCP Plugin 的局部问题，而是通用 Toolset Progressive Loading 暴露出的架构缺口。

## 已确认事实

当前系统存在三个不同生命周期：

```text
持久配置事实
能力是否安装、启用，以及当前 revision

Conversation 历史事实
用户、模型和工具实际发生过的协议轨迹

Turn 可执行事实
当前 Turn 已加载的 ToolSpec、参数 Schema 和临时 Registry
```

现有设计中：

- Session 创建时冻结 `toolset_id → revision`，决定本 Session 可以看到哪些 Toolset；
- `load_toolset` 在当前 Turn 调用 Provider，发现并冻结具体 ToolSpec；
- ToolSpec 随 Turn 结束释放；
- MCP Client 的物理连接与目录缓存由 Application/Plugin 生命周期管理；
- `load_toolset` 的工具结果只记录 `toolset_id`，没有记录本次发现了哪些能力；
- 完整工具 Schema 只进入当前 Turn 的模型 `tools` 字段，从未成为 Conversation 工具结果；
- Level 1 脱水会把保护窗外已消费的成功工具结果替换成 Artifact Stub。

因此，当前缺口可以描述为：

> 动态能力的执行状态有明确的 Turn 生命周期，但能力发现没有形成可跨 Turn 理解和恢复的最小 Conversation 事实。

## 本专题要回答的问题

### 问题一：`load_toolset` 产生了什么事实？

候选解释：

```text
A. 只表示一次命令执行成功
B. 表示当前 Turn 已获得一组可执行工具
C. 同时产生“当时发现了什么”的历史事实
```

需要判断：如果只选择 B，模型在后续 Turn 中凭什么理解上一次发现结果？如果选择 C，哪些信息才是最小且可信的发现事实？

### 问题二：历史发现事实和当前可执行事实如何区分？

Conversation 可能记录：

```text
此前从 toolset `demo` 观察到工具 `demo__echo`
```

但它不能推出：

```text
当前 Turn 已注册 `demo__echo`
```

需要形成一个模型可理解、Runtime 可验证的恢复契约，使模型知道：

```text
历史发现可以用于理解和选择能力
实际调用必须以当前 Turn 暴露的 tools 为准
当前 Turn 未加载时必须重新调用 load_toolset
```

### 问题三：工具结果脱水后必须保留什么？

当前脱水保留 Artifact 地址，却不保留工具结果的业务语义。需要区分：

```text
一次性正文
可以只保存 Artifact 地址

恢复凭据
影响未来行动，脱水后仍需保留最小语义
```

需要判断这是 `load_toolset` 的特殊需求，还是所有工具结果都可能声明的通用投影契约。

## 当前设计假设

以下内容是待测试的假设，不是已经冻结的最终结论：

> ToolSpec 继续保持 Turn-scoped；Toolset discovery 形成 Conversation-scoped 的 capability receipt；后续 Turn 依据当前语义决定是否重新加载。

一个候选 receipt：

```json
{
  "ok": true,
  "code": "TOOLSET_LOADED",
  "data": {
    "toolset_id": "demo",
    "scope": "turn",
    "observed_tools": [
      {
        "name": "demo__echo",
        "description": "Echo a message"
      }
    ],
    "recovery": "Call load_toolset with demo again in a later turn before using its tools."
  }
}
```

候选 receipt 只保存精确名称、简短描述、作用域和恢复动作，不保存完整参数 Schema。当前调用仍必须使用本 Turn 重新发现并注册的真实 ToolSpec。

## 已冻结的阶段决策

### `load_toolset` 契约

- `load_toolset` 会修改当前 Turn 的 `ToolsetLoadingState`，但不持有持久状态；
- 作用域属于工具的稳定契约，由 description/runtime instruction 说明为 Turn-scoped；
- 执行结果返回本次实际发现的完整工具列表；
- 每项只返回 Runtime 暴露给模型的精确 `name` 与 `description`；
- 暂不返回 Provider 原始名称、完整参数 Schema 或 Provider 专属元数据；
- 结果大小服从通用 Tool Result Limit，超限走既有 Artifact 外置，不增加专用分页或截断协议；
- 同一 Turn 重复加载不重新 discovery，基于首次冻结的 ToolSpec 返回相同 receipt。

### 当前与历史可执行性

- 历史 receipt 只证明过去观察到某项能力，不证明当前 Turn 已注册该工具；
- 实际调用只能使用当前轮 `tools` 中暴露的精确名称；
- 当前 Turn 未加载时，由模型根据语义显式调用 `load_toolset`；
- `TOOL_NOT_FOUND` 只报告当前 Turn 不存在该工具，不猜所属 Toolset、不自动改名、不自动加载，也不推断安装状态。

### 持久能力配置变更

不再为新增、更新与撤权维护不同的生效语义。统一规则为：

> 任何持久能力配置发生变化，当前 Session 的能力快照即过期；必须创建新 Session 才能继续使用动态能力。

```text
配置变更
→ 持久配置 revision 变化
→ 当前 Session capability snapshot 过期
→ 后续动态能力加载或调用明确失败
→ /mcp reload 创建捕获最新快照的新 Session
```

`/mcp reload` 属于 Plugin 控制面，不是普通 Agent Tool。它不在旧 Session 内静默替换快照，只负责创建新 Session。该统一规则已由通用 Provider snapshot token、加载边界与工具调用边界实现，并已同步到 Rule 同步区。

## 第一版实现结果

- `load_toolset` 返回完整的精确工具名与描述，并在同一 Turn 重复加载时复用冻结快照；
- Toolset 运行时指令明确区分历史发现事实与当前 Turn 可执行状态；
- `TOOL_NOT_FOUND` 只报告当前 Turn 的机械事实并提示显式加载，不猜测来源；
- `SessionCapabilitySnapshot` 同时冻结可见 Toolset revisions 与 Provider snapshot token；
- `SnapshotToolsetProvider` 在加载和已加载工具调用边界比较完整快照，任意配置变化返回 `SESSION_CAPABILITIES_STALE`；
- MCP Provider token 覆盖全部 Server 的 `id/revision/enabled`，包括当前未启用配置；
- Console 提供 `/mcp reload`，通过创建新 Session 捕获最新 MCP 能力快照；
- 相邻 Turn 自动化契约测试验证 receipt 留在 Conversation、具体 ToolSpec 不跨 Turn 泄漏、下一 Turn 重新加载后才能调用。

Level 1 脱水后的最小恢复语义仍留给实验四，不在本次实现中扩展通用脱水协议。

## 暂不采用的方案

### Session 自动保持全部已加载 ToolSpec

这能让连续追问直接调用工具，但会把 `load_toolset` 从“当前任务按需选择能力”改成“为整个 Session 启用能力”。是否需要改变该产品语义，不能由一次失败直接推出。

### 新增持久 Capability Manifest

Conversation 已经是永久协议事实层。在证明 Conversation 投影无法承担恢复语义前，不新增重复的持久化模型。

### Runtime 根据用户指代自动恢复 Toolset

Runtime 不判断“它”指向哪个历史能力。语义选择仍由模型负责，Runtime 只执行明确的 `load_toolset` 并装配当前 Turn。

### 保存完整 JSON Schema 到 Conversation

完整 Schema 属于当前 Turn 的可执行契约。历史 Conversation 只需要支持理解、选择和恢复，不承担当前调用参数校验。

## 递进实验

### 实验一：原始加载结果是否足以支撑回答

构造与 MCP 无关的 `FakeToolsetProvider`：

```text
load_toolset("demo")
→ 返回精确工具名和描述
→ 模型依据工具结果解释 demo 能做什么
```

验收重点：回答中的能力声明来自工具结果或当前 ToolSpec，不能来自模型先验。

### 实验二：相邻 Turn 的恢复

```text
Turn 1：加载 demo 并完成解释
Turn 2：用户要求实际演示
```

期望模型先重新调用 `load_toolset("demo")`，下一 AgentStep 再调用当前 Turn 暴露的精确工具名。

验收重点：历史 receipt 用于选择恢复动作，不能被当作当前可执行状态。

### 实验三：历史发现与当前 Schema 不一致

```text
Turn 1：观察到 demo__echo
Turn 2：Provider 当前返回 demo__repeat
```

期望模型只能调用 Turn 2 实际暴露的 `demo__repeat`。历史 receipt 是历史事实，不是当前真相。

### 实验四：跨越 Level 1 脱水保护窗

让 Turn 1 的 `load_toolset` 工具结果进入可压缩区，再开始需要恢复该能力的新 Turn。

验收重点：模型上下文是否仍保留 `toolset_id`、历史能力摘要和恢复动作。只有该实验失败后，才设计通用的“脱水保留语义”契约。

### 实验五：任意配置变更统一使 Session 快照失效

```text
Conversation：曾成功加载 demo
持久配置：demo 新增、更新、禁用或删除
```

期望旧 Session 对上述变更采用同一种结果：能力快照过期，动态能力不能继续加载或调用，必须创建新 Session。模型不得根据历史 receipt 绕过快照失效。

## 设计约束

- Core 只理解通用 Toolset、ToolSpec、Conversation 与 ModelContext，不引入 MCP 名词；
- 内部代码相信契约，非法状态直接保留原始异常失败；
- 不通过自动重载、静默刷新或工具名自动改写掩盖模型调用错误；
- Runtime 判断当前注册、revision、Schema 和撤权等机械事实；
- 模型判断当前用户意图是否需要恢复某项历史能力；
- 先以最小契约通过实验，再决定是否扩展通用工具结果投影协议。

## 第一轮学习问题

开始设计实现前，先回答：

> `load_toolset` 成功后，“发现了哪些工具”究竟是工具执行结果的一部分，还是仅仅属于下一个 AgentStep 模型请求的临时配置？

如果它只属于临时配置，Turn 结束后为什么还允许模型在 Conversation 中声明这些工具存在？如果它属于工具执行结果，当前 `TOOLSET_LOADED` 契约又缺少哪些必须记录的事实？
