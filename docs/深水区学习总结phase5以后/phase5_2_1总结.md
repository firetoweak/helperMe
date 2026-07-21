## Phase 5.2.1 Tool Result Budget / Runtime Artifact 总结

这一步是 Phase 5.3 Safe Compression 的前置。我们解决的问题不是简单截断字符串，而是保证单次工具结果不会独自撑爆上下文，同时保留完整结果供模型按需继续读取。

### 为什么 Context Budget 仍然不够

Phase 5.2 已经能在模型请求前判断完整输入是否超过预算，但它只能发现问题，不能消除一个刚产生的巨大 tool message。

如果某次工具返回几十万字符：

```text
旧历史很小
+ 当前巨大 tool message
= 本次输入超过预算
```

此时压缩旧历史没有意义，因为这个新结果本身就是不可再缩小的最大单元。因此 Safe Compression 开始前，必须先建立一个不变量：

> 任何进入 ToolsState、Conversation 和 ModelContext 的单次工具结果都必须有界。

### 两层限制不是重复职责

工具结果采用两层控制：

```text
第一层：工具领域内限流
第二层：Runtime 统一硬上限
```

领域工具最了解自己的结果语义：

- read_file 按行或字符分页；
- grep 限制匹配数量并提供继续定位信息；
- diff 工具可以按文件或区块限制；
- 网页工具可以区分正文、链接和元数据。

Runtime 不理解这些领域结构，不能替工具决定在哪里截断。但第三方工具、旧工具或异常结果仍可能绕过领域限制，所以 Runtime 还需要统一兜底。

统一兜底不直接丢弃结果，而是把完整结果外置，只把有界引用交给模型。

### 最终处理链路

```text
Tool handler
  ↓ 领域内限流 / 分页
ToolsExecutor
  ↓ 执行并标准化为统一结果
ToolResultExternalizer
  ├─ 未超过 16,000 字符：原样返回
  └─ 超过 16,000 字符：
       完整标准化 JSON → RuntimeArtifactStore
       artifact_id + size + 1,200 字符预览 → ToolsState
  ↓
Conversation tool message
  ↓
ContextManager 再次检查硬上限
  ↓
ModelContext
```

`ToolsExecutor` 没有承担截断、存储或上下文管理职责。它只负责调用工具和标准化结果。统一结果策略由独立的 `ToolResultExternalizer` 负责，`RunRuntime` 只编排两者的先后顺序。

### 三种数据形态

这一步把“工具结果”进一步区分为三种形态：

```text
完整工具结果
    存在 RuntimeArtifactStore，保留全部数据

上下文工具结果
    存在 ToolsState / Conversation，内容有界

模型输入投影
    存在 ModelContext，发送前再次验证
```

完整结果和上下文工作集不再是同一个东西。Conversation 保存模型能够继续决策所需的协议事实，但不承担保存任意体积原始数据的责任。

### Runtime Artifact 不是用户文件

RuntimeArtifactStore 必须与用户 Workspace 分离。

如果把 artifact 写入用户项目：

- get_changes 会把系统临时数据识别为用户改动；
- StopGuard 的写入与验证语义会受到干扰；
- 临时结果可能被提交到 Git；
- 模型可能绕过封装直接读取或修改内部文件；
- 更换存储实现时会影响模型工具契约。

因此模型永远看不到 artifact 的真实路径，只能获得不透明的随机 `artifact_id`：

```json
{
  "externalized": true,
  "artifact_id": "art_...",
  "size_chars": 48231,
  "preview": "前 1200 字符……"
}
```

真实路径只存在于 FileArtifactStore 内部。Composition Root 还会拒绝位于用户 Workspace 内的 runtime_root。

### read_artifact 是能力封装

模型通过只读工具读取外置结果：

```text
read_artifact(artifact_id, offset, limit)
```

约束：

- offset 使用字符偏移，不依赖换行；
- 单次最多返回 3,000 字符；
- 返回 next_offset 时可以继续读取；
- 模型不能指定文件路径；
- 模型不能创建、枚举或删除 artifact；
- 非法 ID、非法 limit 和不存在的 artifact 在工具输入边界返回明确错误。

`artifact_id` 是能力句柄，不是存储位置。以后 FileArtifactStore 即使替换成数据库或对象存储，模型侧工具契约也不需要变化。

### 工具注册表为什么需要实例化

原工具注册表依赖模块导入副作用和全局 `_TOOL_SPECS`。普通无状态工具可以勉强使用这种方式，但 read_artifact 必须绑定当前应用自己的 ArtifactStore。

如果继续使用全局注册表，只能选择：

- 创建全局 ArtifactStore；
- 在 ToolsExecutor 中特判 read_artifact；
- 让工具自己读取环境变量并重新创建依赖。

这些方案都会破坏应用实例隔离或组件职责。

现在内置工具仍可以通过装饰器声明，但 Composition Root 会复制出当前应用的 ToolRegistry，再注册绑定当前 ArtifactStore 的 read_artifact：

```text
BUILTIN_TOOL_REGISTRY
    ↓ clone
应用级 ToolRegistry
    ├─ 内置工具
    └─ read_artifact(current ArtifactStore)
```

真正出现了实例依赖消费者以后，注册表实例化才有了实际价值，而不是为了抽象而抽象。

### ContextManager 只检查，不落盘

正常的新工具结果已经在进入 Conversation 前完成外置。ContextManager 的检查用于发现：

- 旧会话中的超大工具消息；
- 第三方路径直接写入的工具消息；
- 异常恢复绕过 Externalizer 的结果。

ContextManager.build() 不会在投影过程中偷偷写 artifact。发现超限就立即失败，因为上下文投影不应该带有文件写入副作用。

未来真正增加会话持久化以后，应由会话加载或迁移边界处理旧的大结果，而不是把迁移职责塞给 ContextManager。

### 失败策略

- ArtifactStore 写入失败属于内部基础设施失败，保留原始异常直接失败；
- 外置后的引用结果如果仍超过硬上限，说明配置契约矛盾，直接失败；
- read_artifact 的非法参数和不存在 ID 属于模型输入边界的预期错误，返回标准工具失败结果；
- 不静默丢弃完整内容，不在失败时偷偷退回原始超大结果。

### 当前实现边界

当前已实现：

- 默认工具结果硬上限 16,000 字符；
- 超限结果完整外置；
- 1,200 字符预览；
- opaque artifact_id；
- read_artifact 字符分页；
- Runtime storage 与用户 Workspace 隔离；
- 应用实例级 ToolRegistry；
- ContextManager 历史结果硬检查；
- RunRuntime 端到端外置链路。

当前明确不做：

- artifact 自动清理；
- 按 Session/Run 划分存储目录；
- artifact 持久化索引；
- 旧会话自动迁移；
- 第三方对象存储；
- 把字符上限替换为复杂的逐工具 token 策略。

这些能力都需要真实的生命周期、恢复或多租户消费者后再设计。

### 验证结果

全量测试 `121/121` 通过，覆盖：

- 小结果不创建 artifact；
- 大结果完整保存且只向 Conversation 暴露引用；
- 引用不包含真实路径；
- 字符分页能够无损拼回完整结果；
- read_artifact 的单次读取上限；
- ArtifactStore 内部异常不被吞掉；
- Runtime root 不能位于用户 Workspace；
- ContextManager 拒绝超大历史 tool message；
- 外置后 tool_call/result 协议链保持完整；
- 既有 Run、Session、Planning 和 StopGuard 行为不变。

这一步最重要的认知是：

> 工具的完整输出属于 Runtime Artifact；模型上下文只保存当前决策需要的有界工作集。领域工具负责语义限流，Runtime 负责统一守住硬边界。
