## Phase 5.3 回补 A：Dynamic TodoList 总结

这一节重新定义了原有 Planning：当前项目需要的不是面向多 Agent 调度的 Plan，而是单个 Run 内面向执行、可动态变化的 TodoList。

最终删除了独立 Planner / Replanner，将初始化、执行期调整和退出前结算统一到同一个 `rewrite_todos` 契约。Executor 仍是唯一认知决策者；Runtime 只保存状态、校验确定性规则并控制退出边界。

### 为什么原有 Plan 效果不理想

旧实现更接近一次性 Todo 清单：

```text
Planner 生成固定步骤
        ↓
Executor 调用工具
        ↓
根据读/写/验证工具机械推进状态
        ↓
只有工具失败时调用 Replanner
```

这导致几个问题：

- 工具成功不等于某个任务阶段已经完成；
- 成功结果带来的新事实不会触发计划调整；
- Replanner 只能整体替换剩余步骤，难以自然表达新增、删除、拆分、合并和调序；
- 最终回答阶段通过批量标记剩余步骤完成，状态并不来自真实执行结果；
- Planner、Executor、Replanner 对同一执行路径重复决策。

根本原因是概念错位：把一个应当频繁变化的执行认知状态命名成了相对稳定的 Plan。

### Plan 与 TodoList 的边界

当前 TodoList 定义为：

> TodoList 是面向执行的、可变的任务认知状态；Executor 将其作为当前行动参考，而不是不可违背的指令序列。

Todo 描述要达成的局部结果，不绑定具体工具调用：

```text
合适：确认消息循环是否完整保存 assistant tool_calls

不合适：调用 read_file(path="core/messages.py")
```

真正的 Plan 留给未来多 SubAgent 阶段：

```text
Plan
├─ WorkUnit A → SubAgent A → TodoList A
├─ WorkUnit B → SubAgent B → TodoList B
└─ WorkUnit C → 依赖 A、B → TodoList C
```

Plan 负责大目标拆分、依赖和委派；TodoList 负责单个 Executor Run 内的执行认知。

### 核心运行原则

本节采用：

> 执行过程柔性，退出边界严格。

```text
外部工具批次完成
        ↓
TodoList 标记为 DIRTY
        ↓
Executor 可以继续调用工具
也可以随时 rewrite_todos
        ↓
模型尝试最终回答
        ↓
Todo Sync Barrier
├─ DIRTY            → 拒绝退出，要求同步
├─ pending / doing  → 拒绝退出，要求继续或取消
└─ done / cancelled → 允许完成 Run
```

Runtime 不强制模型每执行一个工具就更新 Todo，也不要求严格按照数组顺序执行。TodoList 是阶段性同步的认知快照，不是实时流水账。

### 生命周期与同步状态

生命周期和同步状态属于两个不同维度：

```python
class TodoPhase(str, Enum):
    UNINITIALIZED = "uninitialized"
    ACTIVE = "active"
    COMPLETED = "completed"


class TodoSyncState(str, Enum):
    CLEAN = "clean"
    DIRTY = "dirty"
```

合法组合为：

```text
UNINITIALIZED

ACTIVE + CLEAN
ACTIVE + DIRTY

COMPLETED + CLEAN
```

`COMPLETED + DIRTY` 永远非法。

主要迁移：

```text
UNINITIALIZED
    │ 首次 rewrite_todos
    ▼
ACTIVE + CLEAN
    │ 外部工具批次完成
    ▼
ACTIVE + DIRTY
    │ rewrite_todos
    ▼
ACTIVE + CLEAN
    │ Todo Sync Barrier 通过
    ▼
COMPLETED + CLEAN
```

中断不是 TodoPhase。当前 TodoList 仍限定在单个 Run 内，跨 Run 的持久化和恢复不属于本节。

### 统一的 rewrite_todos 契约

初始化和后续修改不再使用两套协议，统一提交完整快照：

```python
class RewriteTodosInput(BaseModel):
    objective: str
    reason: str
    todos: list[RewriteTodoInput]


class RewriteTodoInput(BaseModel):
    id: int | None
    content: str
    status: Literal["pending", "doing", "done", "cancelled"]
    note: str | None
```

字段语义：

- `objective`：当前执行目标摘要；Conversation 仍是用户需求的事实源；
- `reason`：本次创建或同步快照的原因；
- 已有 Todo 保留原 id；
- 新 Todo 使用 `id=null`，由 Runtime 分配单调递增 id；
- 旧 Todo 未出现在新快照中表示删除；
- 数组顺序表示当前建议执行顺序；
- `cancelled` 表示确认不再需要，必须提供 note 说明原因。

完整快照天然支持：

- 状态修改；
- 新增和删除；
- 顺序调整；
- 一项拆成多项；
- 多项合并成一项；
- 取消不再需要的事项。

Runtime 不实现 add/remove/move/split/merge 操作 DSL，避免索引漂移、操作冲突和额外调度语义。

相同快照允许提交：外部工具可能只是验证了原判断，Todo 内容不需要变化，但仍需把 `DIRTY` 同步回 `CLEAN`。此时 revision 不增加。

### Todo 初始化阶段

删除的是独立 Planner 组件，保留的是受限的 Todo 初始化阶段：

```text
同一个模型
+ 完整 Conversation 投影
+ Todo 初始化 Prompt
+ 只开放 rewrite_todos
```

初始化模型必须且只能调用一次 `rewrite_todos`：

- 包含 2 到 6 个 Todo；
- 所有 id 为 null；
- 所有状态为 pending；
- 只形成执行步骤，不执行任务、不回答用户问题。

首次合法快照完成：

```text
UNINITIALIZED → ACTIVE + CLEAN
revision = 1
```

如果模型返回文本、调用其他工具、调用多个工具或提交非法快照，`TodoMode` 以 `invalid_todo_initialization` 拒绝该次激活。显式固定 TodoMode 时当前 Run 失败；动态路由场景由 RunRuntime 记录原因并降级到 PlainMode。Runtime 不生成默认 TodoList，也不把失败的初始化响应写回 Conversation；原始响应只进入错误与 trace，避免掩盖模型协议问题。

### 同一个模型兼任 Executor 与 Todo 审查者

初始化后进入普通 Agent Round：

```text
Conversation
+ 当前 TodoList 运行时投影
+ rewrite_todos
+ 外部工具
        ↓
同一个模型决定下一步
```

不再存在：

```text
工具失败
→ 独立 Replanner 模型调用
→ 生成另一份后续计划
```

工具成功、失败、发现新信息或路径改变都只是新的 observation。Executor 可以继续行动，也可以调用 `rewrite_todos` 重新表达当前最佳执行假设。

### Todo Sync Barrier

每个无 tool_calls 的 Assistant text 在当前协议中都表示一次最终回答候选。Runtime 在真正结束 Run 前调用 Todo Sync Barrier：

```text
final candidate
    ↓
检查 TodoList
├─ sync=DIRTY
│    → 返回动态反馈，不接受当前文本
├─ 存在 pending/doing
│    → 返回未结束 Todo id，不接受当前文本
└─ CLEAN 且全部终结
     → 再执行原有 StopGuard
     → 完成 Run
```

Barrier 只返回结构化判断，不直接修改 Conversation。RunRuntime 负责把反馈追加为 user message 并继续下一轮，从而保持：

```text
Todo 策略负责判断
RunRuntime 负责循环控制和消息写入
```

Todo Sync Barrier 保证的是执行事实与 Todo 快照在退出时一致，不保证 Todo 拆分本身正确，也不替代测试、`get_changes` 等任务验证。

### Prompt 分层

旧实现把以下内容每轮都注入模型：

```text
phase / sync_state / revision / 退出规则
```

其中 phase、sync_state 和 revision 主要用于 Runtime 状态与审计，不是模型完成任务所需的认知内容。

重构后分为两层：

1. 常规 Runtime Instructions
   - 当前目标；
   - Todo 内容、状态和说明；
   - TodoList 是柔性执行认知；
   - 路径或状态实质变化时可重写完整快照；
   - 最终结束前需要完成同步。

2. 动态 Barrier Feedback
   - 只在模型尝试退出且未通过时出现；
   - 明确指出是 DIRTY，还是哪些 Todo 尚未结束；
   - DIRTY 不禁止继续调用外部工具，只阻止最终退出。

工具描述负责说明完整快照语义、id 规则和适合调用的时机，避免把所有行为规则重复堆在 system prompt 中。

### Run-local 状态与无状态 TodoMode

旧 `TodoMode` 既是 Runtime 策略，又通过 `self.todo_list` 持有某个 Run 的可变状态。由于 TodoMode 由 Composition Root 创建并挂在可复用的 RunRuntime 上，这种设计会为多 Session 或未来并发留下状态串线风险。

重构后：

```text
RunRuntime.run()
    ├─ mode_state = TodoMode.create_state()
    ├─ 初始化 TodoList
    ├─ 每轮显式传入 mode_state
    └─ Run 结束后丢弃该状态

TodoMode
    └─ 无 Run 状态，只保存生命周期策略
```

RuntimeMode 接口因此改为显式接收 state：

```text
create_state()
start(state)
accept_start_response(state, response)
runtime_instructions(state)
execute_tool(state, ...)
after_tool_batch(state, ...)
check_final_candidate(state)
on_run_completed(state)
checkpoint_data(state)
```

TodoList 的所有权与其生命周期一致：它属于一个 Run，而不是属于可复用的 Mode。

### 后续增量：Runtime Mode Router

TodoList 落地后，简单问题仍然会无条件进入 Todo 初始化，额外产生一次模型调用和一套不必要的执行状态。现在在每个 Run 的执行入口增加轻量 Router，只判断本次请求需要哪种 RuntimeMode：

```text
Conversation
    │ 追加本次 user message
    ▼
RuntimeModeRouter
    │ 读取完整 Conversation
    │ tools = []
    ▼
{"mode":"plain|todo","reason":"..."}
    ├─ plain → PlainMode → 直接进入 Agent Round
    └─ todo  → TodoMode  → 受限 Todo 初始化 → Agent Round
```

路由属于 Run，不属于 Session。同一个 Session 的简单追问可以选择 `plain`，后续复杂任务仍可重新选择 `todo`。Router 不持有状态，也不改变 Conversation。

Router 只负责两件事：

- 提供区分 `plain/todo` 的 system prompt；
- 严格解析模型返回的结构化决策。

`RunRuntime` 继续统一负责 Context Preparation、模型调用、retry、usage 和 checkpoint。Runtime 不使用步骤数、关键词等规则自行猜测复杂度，也不让 Router 生成 Todo：

```text
Router：选择执行机制
TodoMode：管理 Todo 生命周期规则
Executor：决定具体行动
```

路由输出契约为：

```json
{"mode":"todo","reason":"需要分析、修改并验证多个步骤"}
```

约束如下：

- `mode` 只能是 `plain` 或 `todo`；
- `reason` 必须是非空字符串；
- 只接受恰好包含这两个字段的 JSON object；
- 先判断最后一条用户消息是否明确授权执行，再判断执行复杂度；
- 讨论、评价、解释、询问看法或提出优化方向选择 `plain`，不能因为话题复杂或历史 Run 使用过 Todo 就推断为授权实施；
- 是否授权执行不明确时选择 `plain`；已明确要求执行、但不确定执行复杂度时选择 `todo`；
- 不允许工具调用、Markdown、额外字段或自然语言包裹；
- 非法路由响应记录 `invalid_runtime_mode_route`，随后在同一 Run 降级到 `plain`。

合法结果只写入 `runtime_mode_routed` checkpoint，模型 token 记入 `routing` usage stage，不把路由原因写回 Conversation。这样 Conversation 仍只保存用户与执行 Agent 的协议轨迹，路由决策属于 Run trace。

Composition Root 默认组装：

```text
RuntimeModeRouter
├─ plain → PlainMode
└─ todo  → TodoMode
```

测试和特定调用仍可显式注入固定 `runtime_mode`，此时跳过路由。固定模式与路由模式互斥，避免一个 Run 同时存在两个 mode 来源。

Router 的选择是概率性执行建议，不是不可逆状态迁移。动态选择 `todo` 后，如果受限初始化没有产生合法 `rewrite_todos`，`TodoMode` 仍严格拒绝该响应，但 `RunRuntime` 不再把局部协议不匹配升级为 Run 失败：

```text
todo activation
    │ 非法初始化响应
    ▼
记录 mode activation failed checkpoint
    │ 丢弃受限阶段响应，不写入 Conversation
    ▼
runtime_mode_fallback: todo → plain
    │ 使用正常 Agent Prompt 重新调用
    ▼
继续同一个 Run
```

严格契约与运行恢复因此分层：`TodoMode` 负责判定初始化是否合法，`RunRuntime` 负责动态 mode 激活失败后的单向、有界降级。显式注入固定 `TodoMode` 时没有备选策略，仍保留原有严格失败语义。

### 最终模块职责

```text
core/todos/
├─ todo_list.py
│    TodoItem、TodoList、TodoPhase、TodoSyncState 与领域迁移
│
├─ rewrite_todos.py
│    Pydantic 输入契约、工具描述、完整快照应用
│
├─ prompts.py
│    初始化 Prompt 与常规 TodoList 模型投影
│
├─ exit_barrier.py
│    纯退出判断与反馈结果
│
└─ mode.py
     薄的 Todo 生命周期协调策略

core/runtime_modes/
├─ router.py
│    RunMode、RouteDecision、严格解析与路由 Prompt
├─ plain.py
│    不启用 Todo 生命周期的轻量执行策略
└─ base.py
     RuntimeMode 协议
```

外围职责：

```text
RunRuntime
├─ 追加当前用户消息后为本次 Run 选择 RuntimeMode
├─ 创建 Run-local mode state
├─ 准备初始化与 Agent ModelContext
├─ 合并 Runtime 工具和外部工具
├─ 执行工具并记录 ToolsState
├─ 把 Mode 反馈写入 Conversation
└─ 驱动安全退出
```

### 工具 Schema 边界补强

`RewriteTodosInput` 包含嵌套的 `todos[]` Pydantic 模型。重构时发现旧 `ToolSpec.to_openai_tool()` 只保留顶层 `properties/required`，会丢弃 Pydantic 生成的 `$defs`，使数组元素中的 `$ref` 无法解析。

现在直接保留完整 `model_json_schema()`：

```text
parameters
├─ properties
├─ required
└─ $defs
    └─ RewriteTodoInput
```

同时，Pydantic ValidationError 输出去掉不可 JSON 序列化的 exception context，保证模型参数错误能稳定转换为标准工具失败，而不是在错误格式化阶段再次崩溃。

### 本节明确不做

- 不增加 Todo `blocked` 状态。当前允许 blocked Todo 退出会与 `RunStatus.COMPLETED` 冲突，需要和 partial/blocked 退出语义一起设计；
- 不引入独立 TodoReviewer；
- 不实现 Todo patch DSL；
- 不根据任务复杂度增加新的隐式 Planner；
- 不把 TodoList 持久化到 Session；
- 不实现跨 Run 的 Todo 恢复；
- 不让 Runtime 判断 Todo 内容是否合理或事项是否真的必要；
- 不用 Todo Sync Barrier 替代工具验证和业务安全检查。

### 验证结果

全量 175 项测试通过，覆盖：

- 初始化阶段只开放 `rewrite_todos`；
- 初始化必须调用且只调用一次该工具；
- 非法初始化保留原始模型响应并明确失败；
- 首次快照的数量、id 和 status 约束；
- 完整快照的新增、删除、修改、重排和稳定 id；
- cancelled 必须填写 note；
- 最多一个 doing；
- 相同快照清除 DIRTY 且不增加 revision；
- 外部工具后进入 DIRTY，但仍允许继续执行；
- DIRTY 或 pending/doing 阻止最终回答；
- TodoMode 不持有 TodoList；
- 同一个 RunRuntime 连续执行多个 Run 时 Todo 状态互不泄漏；
- RuntimeMode、PlainMode、Context Preparation、StopGuard、interrupt 和 Session 原有链路保持正常；
- 嵌套 Pydantic 工具 schema 保留完整 `$defs`。
- Router 严格解析 `plain/todo` 决策，非法响应不降级；
- Router 读取包含当前请求的完整 Conversation，但决策不写回 Conversation；
- plain 跳过 Todo 初始化，todo 保持原有初始化与退出屏障；
- 同一个 Session 的不同 Run 可以选择不同 mode；
- Todo Run 完成后的方案讨论能够重新路由到 plain，不继承上一轮模式；
- 路由结果、原因和模型 usage 进入 Run checkpoint。
- 非法路由或动态 Todo 激活失败会记录原因并在同一 Run 降级到 plain；
- Todo 初始化阶段产生的非法文本不会进入 Conversation，也不会杀死当前 Session。

这一节最重要的认知是：

> TodoList 是 Executor 当前最佳执行假设的快照；模型负责根据 observation 改写假设，Runtime 只维护快照契约和退出一致性。初始化、调整与结算使用同一个 rewrite 操作，才能避免 Planner、Executor、Replanner 之间重复决策和状态漂移。
