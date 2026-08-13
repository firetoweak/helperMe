# 动态能力跨 Run 连续性设计专题

## 专题背景

Phase 6B 的真实 MCP 测试出现了以下连续对话：

```text
Run 1
用户：mcp-everything 是干嘛用的？
Agent：load_toolset("mcp:mcp-everything")
Runtime：发现工具并把 ToolSpec 注册到当前 Run

Run 2
用户：你能用它做什么有趣的事情？
Agent：根据历史回答构造裸工具名
Runtime：TOOL_NOT_FOUND
Agent：错误推断 MCP 尚未安装
```

MCP 安装、Session 能力快照、连接和 `tools/list` 实际均已成功。失败发生在动态能力跨 Run 延续时，因此这不是 MCP Plugin 的局部问题，而是通用 Toolset Progressive Loading 暴露出的架构缺口。

## 已确认事实

当前系统存在三个不同生命周期：

```text
持久配置事实
能力是否安装、启用，以及当前 revision

Conversation 历史事实
用户、模型和工具实际发生过的协议轨迹

Run 可执行事实
当前 Run 已加载的 ToolSpec、参数 Schema 和临时 Registry
```

现有设计中：

- Session 创建时冻结 `toolset_id → revision`，决定本 Session 可以看到哪些 Toolset；
- `load_toolset` 在当前 Run 调用 Provider，发现并冻结具体 ToolSpec；
- ToolSpec 随 Run 结束释放；
- MCP Client 的物理连接与目录缓存由 Application/Plugin 生命周期管理；
- `load_toolset` 的工具结果只记录 `toolset_id`，没有记录本次发现了哪些能力；
- 完整工具 Schema 只进入当前 Run 的模型 `tools` 字段，从未成为 Conversation 工具结果；
- Level 1 脱水会把保护窗外已消费的成功工具结果替换成 Artifact Stub。

因此，当前缺口可以描述为：

> 动态能力的执行状态有明确的 Run 生命周期，但能力发现没有形成可跨 Run 理解和恢复的最小 Conversation 事实。

## 本专题要回答的问题

### 问题一：`load_toolset` 产生了什么事实？

候选解释：

```text
A. 只表示一次命令执行成功
B. 表示当前 Run 已获得一组可执行工具
C. 同时产生“当时发现了什么”的历史事实
```

需要判断：如果只选择 B，模型在后续 Run 中凭什么理解上一次发现结果？如果选择 C，哪些信息才是最小且可信的发现事实？

### 问题二：历史发现事实和当前可执行事实如何区分？

Conversation 可能记录：

```text
此前从 toolset `demo` 观察到工具 `demo__echo`
```

但它不能推出：

```text
当前 Run 已注册 `demo__echo`
```

需要形成一个模型可理解、Runtime 可验证的恢复契约，使模型知道：

```text
历史发现可以用于理解和选择能力
实际调用必须以当前 Run 暴露的 tools 为准
当前 Run 未加载时必须重新调用 load_toolset
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

> ToolSpec 继续保持 Run-scoped；Toolset discovery 形成 Conversation-scoped 的 capability receipt；后续 Run 依据当前语义决定是否重新加载。

一个候选 receipt：

```json
{
  "ok": true,
  "code": "TOOLSET_LOADED",
  "data": {
    "toolset_id": "demo",
    "scope": "run",
    "observed_tools": [
      {
        "name": "demo__echo",
        "description": "Echo a message"
      }
    ],
    "recovery": "Call load_toolset with demo again in a later run before using its tools."
  }
}
```

候选 receipt 只保存精确名称、简短描述、作用域和恢复动作，不保存完整参数 Schema。当前调用仍必须使用本 Run 重新发现并注册的真实 ToolSpec。

## 暂不采用的方案

### Session 自动保持全部已加载 ToolSpec

这能让连续追问直接调用工具，但会把 `load_toolset` 从“当前任务按需选择能力”改成“为整个 Session 启用能力”。是否需要改变该产品语义，不能由一次失败直接推出。

### 新增持久 Capability Manifest

Conversation 已经是永久协议事实层。在证明 Conversation 投影无法承担恢复语义前，不新增重复的持久化模型。

### Runtime 根据用户指代自动恢复 Toolset

Runtime 不判断“它”指向哪个历史能力。语义选择仍由模型负责，Runtime 只执行明确的 `load_toolset` 并装配当前 Run。

### 保存完整 JSON Schema 到 Conversation

完整 Schema 属于当前 Run 的可执行契约。历史 Conversation 只需要支持理解、选择和恢复，不承担当前调用参数校验。

## 递进实验

### 实验一：原始加载结果是否足以支撑回答

构造与 MCP 无关的 `FakeToolsetProvider`：

```text
load_toolset("demo")
→ 返回精确工具名和描述
→ 模型依据工具结果解释 demo 能做什么
```

验收重点：回答中的能力声明来自工具结果或当前 ToolSpec，不能来自模型先验。

### 实验二：相邻 Run 的恢复

```text
Run 1：加载 demo 并完成解释
Run 2：用户要求实际演示
```

期望模型先重新调用 `load_toolset("demo")`，下一 Round 再调用当前 Run 暴露的精确工具名。

验收重点：历史 receipt 用于选择恢复动作，不能被当作当前可执行状态。

### 实验三：历史发现与当前 Schema 不一致

```text
Run 1：观察到 demo__echo
Run 2：Provider 当前返回 demo__repeat
```

期望模型只能调用 Run 2 实际暴露的 `demo__repeat`。历史 receipt 是历史事实，不是当前真相。

### 实验四：跨越 Level 1 脱水保护窗

让 Run 1 的 `load_toolset` 工具结果进入可压缩区，再开始需要恢复该能力的新 Run。

验收重点：模型上下文是否仍保留 `toolset_id`、历史能力摘要和恢复动作。只有该实验失败后，才设计通用的“脱水保留语义”契约。

### 实验五：撤权优先于历史 receipt

```text
Conversation：曾成功加载 demo
当前目录：demo 已禁用或删除
```

期望当前 Runtime 目录成为唯一可执行事实。模型不得根据历史 receipt 调用、恢复或建议绕过撤权。

## 设计约束

- Core 只理解通用 Toolset、ToolSpec、Conversation 与 ModelContext，不引入 MCP 名词；
- 内部代码相信契约，非法状态直接保留原始异常失败；
- 不通过自动重载、静默刷新或工具名自动改写掩盖模型调用错误；
- Runtime 判断当前注册、revision、Schema 和撤权等机械事实；
- 模型判断当前用户意图是否需要恢复某项历史能力；
- 先以最小契约通过实验，再决定是否扩展通用工具结果投影协议。

## 第一轮学习问题

开始设计实现前，先回答：

> `load_toolset` 成功后，“发现了哪些工具”究竟是工具执行结果的一部分，还是仅仅属于下一轮模型请求的临时配置？

如果它只属于临时配置，Run 结束后为什么还允许模型在 Conversation 中声明这些工具存在？如果它属于工具执行结果，当前 `TOOLSET_LOADED` 契约又缺少哪些必须记录的事实？
