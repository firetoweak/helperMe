# Agent Runtime Durable MVP 实现总结

> 状态：14.2 Durable 切片已完成（2026-08-21）  
> 权威设计：[Agent Runtime 状态推进模型](Agent%20Runtime状态推进模型.md)  
> 前置切片：[Agent Runtime 语义 MVP 实现总结](Agent%20Runtime语义MVP实现总结.md)  
> 后续实现：[Agent Runtime 边界切片实现总结](Agent%20Runtime边界切片实现总结.md)

## 1. 本次结论

`agent_runtime/` 已从单进程语义原型推进为可持久、可竞争认领、可重启恢复的最小 Runtime。实现仍与旧 `core.*` 隔离，没有双写或兼容旧 `TurnRuntime`。

本次没有改变 14.1 的职责划分，而是把原有语义落到 Durable 边界：

```text
Event     → SQLite Journal 中的唯一持久事实
State     → Event 的确定性归约，可删除后重建
Step      → 带租约栅栏的原子决策提交
Command   → Attempt / Receipt / Reconcile / Outcome 副作用闭环
Turn      → 可从 Journal 重建的人类视图
```

## 2. 实现结构

14.1 的模块继续保留；14.2 主要新增或强化：

```text
agent_runtime/
├── codec.py            稳定事件标签、严格 schema 与 State codec
├── sqlite_journal.py   SQLite Journal、协调索引与原子事务
├── journal.py          Journal 协议及同语义 MemoryJournal
├── dispatcher.py       Attempt / Reconcile 租约、心跳与恢复
├── state.py            Durable Attempt 状态及迟到事实归约
└── runtime.py          Checkpoint、恢复入口与持久 Journal 门面
```

SQLite 使用 WAL、`synchronous=FULL`、外键和短事务。当前支持同一主机上多个 Worker 共享一个本地数据库文件；不承诺网络文件系统或跨主机分布式锁语义。

## 3. 已闭合的 Durable 不变量

### 3.1 Event 与 delivery

- Event envelope、payload 和整棵持久对象图采用闭合 schema；构造成功的 Event 可以稳定编码、重启读取并保持相等。
- Event type 使用稳定显式标签，不依赖 Python 类名偶然变化。
- 外部 UserMessage / Interrupt 必须带稳定 `DeliveryIdentity`；相同 delivery 内容重投返回原 Event，内容冲突直接失败。
- 外部 delivery 接口不能写内部执行事实；内部条件提交也不能占用 delivery identity。
- Journal 只写当前受支持的事件 schema，未来版本不会被旧程序静默降级。

### 3.2 Step

- Step claim 持久保存 token、owner、generation、冻结状态版本和过期时间。
- 同一决策 Event 最多成功提交一个 Step；旧 Worker 的过期 claim 无法 Commit。
- Decision、Commands 与决策消费位置在同一事务提交。
- 调用协程被取消时，已落库但尚未返回的 claim 会完成事务并补偿释放；重复取消也不会遗留永久 claim。

### 3.3 Command 与 Attempt

- `pending → DispatchAttemptStarted` 是数据库内的原子认领，外部工具调用只能发生在 Attempt 已持久化之后。
- 每个 Attempt 拥有稳定身份、attempt number、claim token 和执行租约；多 Worker 不能为同一派发资格创建两个有效 Attempt。
- Tool Recovery Contract 随 Command 持久化；重启后 Adapter 声明与原契约不一致会直接失败。
- 每个 Attempt 最多接受一个 terminal fact；相同结果可幂等重放，冲突结果不能覆盖先到事实。
- 模型决定 retry 时会同时冻结目标 Attempt 身份，迟到 receipt 不能被旧决策错误覆盖。

### 3.4 Reconcile 与恢复

| Command 状态 | Durable 行为 |
|---|---|
| `pending` | 原子重新调度并先写 Attempt |
| `unknown` | 等执行租约失效后，按持久 Recovery Contract reconcile；不能查询时生成受约束的 RecoveryRequired |
| `running` | 使用持久 external operation id 查询；查询由独立 reconcile 租约单飞执行 |
| `terminal` | 不再执行 Command 恢复动作，仍接纳合法迟到事实 |

直接工具调用返回的 Accepted / Terminal 是外部原始事实，即使本地执行租约刚好失效也不能丢弃。Reconcile 结果是带时效的观察，只允许当前有效 reconcile 租约提交；旧查询结果、旧 NoEffect 和旧 RecoveryRequired 都会被条件事务拒绝。

迟到的真实 receipt 可以纠正先前的 NoEffect 观察，使 Attempt 回到 `RUNNING`，并同步撤销重新派发资格。

### 3.5 Durable CancelTool

CancelTool 是本地编排结果，不被伪装成外部原始事实：

- 对仍为 `pending` 的目标，Journal 在一个事务中同时提交“目标 CANCELLED”和“Cancel Command SUCCEEDED”；
- 该事务重新验证目标派发资格及 Cancel Attempt / Reconcile 租约，因此目标启动与取消不会同时成功；
- 重启时先恢复已经存在的 Cancel Attempt，再启动普通 pending Command；
- 目标执行状态无法在重启后确认时，Cancel Attempt 进入 RecoveryRequired，不声称已经取消；
- 若取消请求之后目标已经形成 CANCELLED 终态，恢复按“取消目标已满足”闭合 Cancel Command。

## 4. Checkpoint 与 Replay

Checkpoint 绑定：

```text
stream_id
journal_position
event fingerprint
state codec version
projection version
```

它只是精确位置上的缓存。版本、位置或指纹不匹配时直接退回完整 Journal replay；删除 Checkpoint 后得到相同 Canonical State。

## 5. 测试

Durable 与语义测试位于：

- `tests/agent_runtime/test_durable_slice.py`
- `tests/agent_runtime/test_semantic_slice.py`
- `tests/test_agent_runtime_suite.py`

当前专项套件共 52 项，覆盖：

- delivery 跨 Worker、跨重启去重与冲突；
- Step / Attempt / Reconcile 的竞争认领、心跳续租、租约过期和 stale worker 栅栏；
- Memory / SQLite 的全局 Step token 约束等价性；
- `pending / unknown / running / terminal` 恢复；
- handler 原始事实与 reconcile 时效观察的竞争；
- NoEffect、迟到 receipt、冻结 retry 和 terminal CAS；
- pending Cancel 的原子竞争及 Cancel Attempt 崩溃恢复；
- 双重协程取消、Windows 读线程句柄收口、schema 封闭、深不可变、codec golden value；
- Checkpoint 命中、删除和完整重放一致性。

验证命令：

```text
python -m unittest tests.test_agent_runtime_suite
python -m unittest
```

最终结果：Agent Runtime 专项 52 项全部通过；全仓回归 502 项通过，2 项按原条件跳过。

## 6. 明确边界

本次不声称外部副作用 exactly-once。它保证先记录 Attempt、保留 unknown、遵守工具恢复契约，并通过 receipt、幂等身份、查询或人工决策继续闭环。

以下内容曾列入 14.3。其中授权门、Interrupt 不可跨越、输出投递边界和 Artifact 缺失降级已完成；Completion / Termination、Turn 产品映射和旧系统迁移仍后置：

- Authorization / Approval 的持久事实与派发门（14.3 已落地为授权门，不含审批工作流）；
- Completion / Termination 的 Finalization Barrier（仍留白）；
- 已提交用户输出的可靠投递（14.3 已把投递收敛为 Command，预览不得进入 Journal）；
- Artifact Store 与精确 Context Replay Manifest（14.3 已做缺失降级；完整 Manifest 仍后置）；
- Turn、Stream 与产品交互对象的正式映射；
- 旧 HelperMe 的 Adapter、迁移、灰度与最终替换。

14.2 的结论是：Durable 能力已经从同一套 Event / State / Step / Command 语义自然长出，不需要回到 Turn 驱动或额外维护第二份 Runtime State。
