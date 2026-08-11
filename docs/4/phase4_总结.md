## Phase 4 · Agent Application Layer 总结

为什么做：Phase 3 后旧 Agent 仍绑定单个 Session，并混合依赖创建、Prompt、用例编排和日志职责，不利于多个入口复用。

目标：建立无状态 `AgentApplication`。Console/API 持有 session_id，应用层通过显式用例操作 SessionRuntime；不改变 Run/Session 语义。

### 学习内容

1. Application Service 与显式用例。
2. Composition Root 与依赖注入。
3. Channel State、Prompt、Observability 边界。

### 职责关系

```text
Console / API -> AgentApplication -> SessionRuntime -> RunRuntime
                    ↑
             Composition Root

Observability <- SessionRunOutcome
```

✓ AgentApplication：提供 create_session、start、resume、request_interrupt；不持有当前 Session、conversation 或 last_result。

✓ Composition Root：统一组装 LLMClient、RunRuntime、SessionRuntime、Prompt 和 AgentApplication。

✓ Console：持有 session_id/run_id，根据上次 RunStatus 显式选择 start 或 resume。

✓ Prompt：从应用服务中拆出，由组合入口选择并注入；以后可扩展为外部人格配置。

✓ Observability：只消费 SessionRunOutcome，不为日志或展示向 SessionRuntime 增加查询接口。

✓ 删除旧 core/agent.py，不保留第二套正式 API。

### 约束

- 自下而上扩展；不因上层展示需求修改 SessionRuntime 以下的边界。
- 不引入 AgentCommand、Context/Memory、持久化 RunState、revision、async、调度、插件系统或 Event Bus。
- start 不隐式创建 Session，错误 session_id/run_id 立即失败。

### Benchmark

- 同一个 AgentApplication 可操作两个 Session，conversation 不串线。
- AgentApplication 不直接创建 LLMClient、RunRuntime，不包含 Prompt 常量和日志写入。
- Console 保持同 Session 多轮与 interrupt/resume。
- Phase 3/4 全量 91 项测试通过。

### Phase 6A Plugin 集成（2026.08.11）

Goal 作为可选 Plugin 通过公共应用端口运行：

- Goal Plugin 的 Composition Root 创建 `GoalStore` 与 `GoalApplicationService`，并显式注入通用 `RunHost` 和 `max_goal_turns`。
- GoalApplicationService 编排 Contract Compilation、Executor Turn、隔离 Judge Run 与 continuation，不把 Goal 语义下沉到 SessionRuntime。
- `RunInvocation` 只提供通用的 Capability 与 RuntimeMode 单 Run 覆盖，不含 Goal 领域词汇。
- AgentApplication 实现与 Goal 语义无关的 RunHost 公共端口，GoalApplicationService 只依赖该端口。
- 架构测试禁止 Core 反向导入 Plugin。

正常消费者和端到端测试只使用公共端口，不读取私有依赖。
