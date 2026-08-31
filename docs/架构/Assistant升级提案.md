# Assistant 升级提案

> 状态（2026-08-31）：提案，未开工。
> 本文只描述问题、边界和候选实施顺序，不构成实现授权。
> 主要范围：`helperme/assistant/`、MCP / Skill 与 Assistant 的装配边界。
> Runtime 工具结果引用若要增强，需要另行确认窄接口变更，见 §4.6。

---

## 0. 结论

当前 Assistant 没有一个需要整体推翻的错误架构。Runtime 仍保持 Event 为唯一持久执行事实，MCP Toolset、Skill 和管理工具也都遵守渐进式加载。

真正需要解决的是三组已经出现的变化扩散：

1. **模型本轮可见内容分散组装**：schema、catalog instruction、控制工具分类分别由 `decision.py` 认识具体来源并拼接。
2. **控制操作缺少单一注册事实**：proposal spec、approval handler、管理域归属和名称占用在装配层分别维护。
3. **可恢复 Host 投影由调用方手挑**：`resume_session()` 直接认识 ToolSurface 和 ManagementSurface。

这些问题彼此相关，但变化原因不同。本轮不建立统一 Plugin / Capability 生命周期，也不把 MCP 与 Skill 的领域实现合并。建议分别引入：

- `PreparedDecisionRequest`：一次模型调用的最终可见输入；
- `ControlOperation`：一项控制操作的完整注册；
- `SessionProjection`：能够从 Journal 重建的 Host 投影窄端口。

三者只收当前已经存在的共性，互不继承，不组成通用插件框架。

---

## 1. 必须保持的现有契约

### 1.1 Runtime 边界

Event 是唯一持久执行事实；State、Context、Replay Manifest 和 Assistant 内存缓存都不是第二事实源。Runtime 不认识 MCP、Skill、管理域或控制审批。

本轮 Assistant 组合重构不改变 Step、Command、Dispatcher 和 Journal 的推进语义。

### 1.2 渐进式加载

- MCP 开工只暴露目录与 `load_toolset`；成功 Outcome 提交后，具体工具从下一 Step 可见。
- MCP / Skill 管理能力开工只暴露管理域目录与 `load_management_tools`；成功 Outcome 提交后，该域诊断工具和控制提案从下一 Step 可见。
- Skill 正文仍通过 `load_skill` / `read_skill_resource` 按需读取，不变成 Toolset 或独立执行循环。

任何重构都必须保持 schema 序列、instruction 内容和下一 Step 才可见的纪律。

### 1.3 控制审批是易失 Host 状态

`AssistantControlPlane._staged / _pending / _active_sessions` 当前只活在进程内。进程退出会放弃未确认方案；恢复 Session 后必须重新提案，绝不自动执行。

这是已声明的产品契约，不在本轮改成 Runtime Event，也不引入审批恢复。

### 1.4 MCP 与 Skill 保持独立

MCP 管理的是外部 Server 配置、连接和运行可用性；Skill 管理的是指令包、来源、hash、diff、启用与修复。两者的 Application Service、Registry 和生命周期不建立共同基类。

允许共享的是 Assistant 控制协议的机械骨架，不是领域状态机。

---

## 2. 已确认的问题

### 2.1 模型可见面由 `decision.py` 枚举具体来源

`JournalBackedLlmDecisionMaker._schemas()` 当前分别处理：

```text
ToolSurface
  + SkillToolAdapter
  + ManagementSurface
  + AssistantControlPlane
```

`decide()` 又独立拼接 Toolset 与 Management 的 catalog instruction。于是同一次模型调用的 schema、说明文字、允许调用名称和控制工具名称没有共同结果对象。

这带来三个问题：

- 增加新的模型可见来源需要修改决策主链路；
- schema 与 instruction 可能分别漏接；
- Management 门控 Control 这条真实依赖藏在 `_schemas()` 的条件逻辑里。

问题不是 `if` 数量本身，而是决策编排器知道每种具体能力来源。

### 2.2 控制操作注册信息分散

当前七项 MCP / Skill 控制操作在装配时分别进入：

```text
AssistantControlPlane.specs
AssistantControlPlane.handlers
ManagementDomain.control_names
ToolSurface.reserved_names
```

这里需要澄清：spec tuple 与 handler tuple **不按位置配对**。`AssistantControlPlane` 分别按 `spec.name` 和 `handler.action` 建表；真正关联发生在 proposal 返回 `ControlApprovalRequest.action` 后。

真实缺口是：系统没有一个对象同时声明以下事实：

```text
模型调用哪个 proposal spec
proposal 预期产生哪个 action
哪个 handler 执行批准结果
它属于哪个 management domain
它占用哪个模型工具名
```

因此可能出现：

- spec 已加入控制面，但没有加入管理域，模型永远看不见；
- proposal 返回的 action 没有 handler，直到 Step 提交后才失败；
- 新名称没有进入 ToolSurface 冲突集合，动态 Toolset 加载时才暴露冲突。

### 2.3 `resume_session()` 手工选择可恢复投影

当前恢复逻辑直接调用：

```python
await surface.rehydrate(session_id, events)
await management.rehydrate(session_id, events)
```

两者虽然恢复内容不同，但语义一致：都从已提交的加载 Command Outcome 重建可丢弃的 Host 投影。

- ToolSurface 从 `load_toolset` 成功 Outcome 恢复 Toolset 激活与绑定；
- ManagementSurface 从 `load_management_tools` 成功 Outcome 恢复管理域激活。

ControlPlane 不属于这组投影，因为它按既定契约故意不恢复。当前代码的问题不是没有恢复 Control，而是新增第三个可恢复投影时只能依靠调用方记得修改 `resume_session()`。

### 2.4 决策方法混合了多个独立输出

`JournalBackedLlmDecisionMaker.decide()` 当前同时负责：

1. 构造本轮模型可见 schema 和 prompt；
2. 取得冻结位置以内的 Journal 事件；
3. 投影模型消息；
4. 估算并记录 context usage；
5. 调用 LLM；
6. 校验和翻译模型响应；
7. 注入 deliver Command；
8. 构造并保存 Replay Manifest。

它仍然应该是模型决策的应用编排器，但“最终模型请求”和“Replay Manifest”已经是可独立命名、独立测试的产物，不应继续以局部变量和字面量 dict 存在。

### 2.5 MCP / Skill 的控制审批骨架重复

`helperme/mcp/approval.py` 和 `helperme/skills/approval.py` 反复实现同一机械流程：

```text
校验 proposal 输入
→ 准备冻结 payload
→ 创建 request id
→ 填 action / summary / risk
→ 创建独占 control ToolSpec
→ 定义对应 approval handler
→ 执行批准后的领域操作
→ 返回 ControlApprovalExecution
```

这部分是真实重复，且与 §2.2 的分散注册互相强化。

但不能据此推导 MCP / Skill 大部分代码都可合并。两边的 Application、Registry、候选冻结、连接测试、hash 与 revision 规则具有不同变化原因。本提案不预估可删除行数，也不以类数量作为抽象依据。

### 2.6 工具结果 artifact 缺少强引用通道

四类 Tool handler 调用 `externalize_payload()` 后只返回替换后的 payload，丢弃了同时产生的 `artifact_id`。externalized stub 本身包含该 ID，因此事实内容并非完全不可追踪；但对应 `CommandOutcomeReceived` Event 的 `artifact_refs` 没有记录它。

正确时序是：

```text
StepCommitted
  → Dispatcher 执行 Tool handler
  → handler 外置大结果并产生 artifact_id
  → CommandOutcomeReceived
```

因此这些引用不能放进 `RecordedDecision.artifact_refs`，因为 Step 提交时 artifact 尚未产生。若要让 Event metadata 强引用工具结果 artifact，需要让 Tool handler 的终态返回值携带 `artifact_refs`，再由 Dispatcher 写入 Outcome Event。

这会改变 Runtime 的 Tool handler / Dispatcher 窄接口，不能伪装成纯 Assistant 修改，见 §4.6。

### 2.7 `DeliveringDecisionMaker` 未进入生产装配

`DeliveringDecisionMaker` 当前只被测试引用，生产链直接在 `JournalBackedLlmDecisionMaker.decide()` 中调用 `ensure_deliver()`。

这个装饰器还只声明返回 `ModelDecision`，不能直接透明包裹当前可能返回 `RecordedDecision` 的 DecisionMaker。它不是备用接线点，而是与当前生产契约已经分叉的死代码候选。

删除前只需用引用扫描和行为测试确认没有外部入口依赖；无需为保留抽象而修复它。

---

## 3. 本轮目标与非目标

### 3.1 目标

- 一次模型调用的最终 prompt、schemas、允许名称和控制名称由一个对象表达；
- 一项控制操作只注册一次，其他集合从注册对象派生；
- Session 恢复遍历显式的可恢复投影集合；
- Management 门控 Control 成为一个有名字、可单测的组合规则；
- Replay Manifest 有独立构造边界；
- MCP / Skill 只共享已经重复的控制协议骨架；
- 重构前后模型可见内容、Command 和 Journal 事实保持等价。

### 3.2 非目标

- 不创建 Plugin、Capability 或 Extension 基类；
- 不统一 MCP / Skill Application、Registry 或生命周期；
- 不持久化控制审批；
- 不把全部能力开工铺给模型；
- 不让 Runtime 理解工具返回体中的领域字段；
- 不以减少文件数、类数或代码行数作为成功标准；
- 不借本轮预建 Automation、SubAgent 或 Long Memory 的接口。

Automation 是外部唤醒和后台 Job 方向，尚不能假定它一定是一种新的模型工具面，不能拿它作为通用 Capability 抽象的先验依据。

---

## 4. 候选设计

### 4.1 给最终模型请求命名

引入只描述一次 LLM 调用可见输入的数据对象：

```python
@dataclass(frozen=True, slots=True)
class PreparedDecisionRequest:
    prompt: str
    schemas: tuple[dict[str, object], ...]
    allowed_tool_names: frozenset[str]
    control_names: frozenset[str]
```

它不重复 `DecisionFrame` 已有的 trigger、cursor、basis version 和 observed position，也不包含装配期的 `reserved_names`。

再引入一个很窄的贡献结果：

```python
@dataclass(frozen=True, slots=True)
class DecisionContribution:
    schemas: tuple[dict[str, object], ...]
    instructions: tuple[str, ...] = ()
    control_names: frozenset[str] = frozenset()


class DecisionContributor(Protocol):
    def contribute(self, frame: DecisionFrame) -> DecisionContribution: ...
```

这里统一的只是“向本轮模型请求贡献什么”，不包括 bindings、reserved names、rehydrate 或领域生命周期。

当前可由三个贡献者组成：

1. `ToolsetDecisionContributor`：基础工具、Toolset loader、已加载 Toolset schema 与目录说明；
2. `SkillDecisionContributor`：当前启用 Skill 的两个普通工具；
3. `ManagementDecisionContributor`：Management schema、目录说明，以及被 Management 显式门控后的 Control schema 和 control names。

Management 与 Control 放在同一个贡献者中，是因为“已加载管理域决定哪些控制提案可见”是一条真实业务规则，不应依赖贡献者遍历顺序或注释表达。

`DecisionRequestBuilder` 负责：

- 按声明顺序合并 contribution；
- 拼出最终 prompt；
- 深拷贝 schema 快照；
- 一次性校验工具名非空且全局唯一；
- 生成 `allowed_tool_names`；
- 校验 `control_names` 是本轮 schema 名称的子集。

`JournalBackedLlmDecisionMaker` 只消费最终的 `PreparedDecisionRequest`，不再认识 ToolSurface、Skill、Management 和 Control 四种具体来源。

### 4.2 用 `ControlOperation` 建立单一注册事实

在不依赖 Assistant 的中立控制协议层定义，并把当前位于 Assistant 的 handler 协议下沉为只描述 `action + execute` 的窄协议：

```python
class ApprovalExecutor(Protocol):
    action: str

    async def execute(
        self,
        payload: Mapping[str, object],
    ) -> ControlApprovalExecution: ...


@dataclass(frozen=True, slots=True)
class ControlOperation:
    domain_id: str
    proposal_spec: ToolSpec
    approval_handler: ApprovalExecutor

    @property
    def action(self) -> str:
        return self.approval_handler.action
```

MCP composition 输出 MCP 的 operation tuple，Skill composition 输出 Skill 的 operation tuple。Assistant 装配层只组合 operation，不再分别抄 spec、handler 和 control name。

派生关系变为：

```text
ControlOperation.proposal_spec
  → AssistantControlPlane 可调用 spec

ControlOperation.approval_handler
  → AssistantControlPlane 批准执行表

ControlOperation.domain_id + proposal_spec.name
  → ManagementDomain 的控制工具可见性

所有已注册 proposal_spec.name
  → ToolSurface 的动态工具冲突集合
```

`AssistantControlPlane` 暂存的不再只是 `_StagedCall(spec, input_data)`，而是对应 operation。proposal handler 返回 `ControlApprovalRequest` 时，必须验证：

```python
request.action == operation.action
```

这样 action 与 handler 的错误关联会在方案准备时立即暴露，不延迟到用户批准后。

`reserved_names` 仍然是 ToolSurface 的装配期输入，但它应从已经装配的静态 schema、Skill tool 名、Management tool 名和 ControlOperation 名称派生，不再直接从多个模块逐个导入常量。

### 4.3 只抽取真正可恢复的投影

定义：

```python
class SessionProjection(Protocol):
    async def rehydrate(
        self,
        session_id: str,
        events: Sequence[Event],
    ) -> object: ...
```

`resume_session()` 接收 `Sequence[SessionProjection]` 并遍历恢复。当前注册：

```text
ToolSurface
ManagementSurface
```

ControlPlane 不实现该协议，也不需要 `RecoveryPolicy.VOLATILE` 占位。它的易失语义通过入口文档和测试明确保持：重启后没有 pending approval，且不会自动执行旧提案。

这个协议只解决恢复调用方枚举问题，不进入决策贡献协议，也不暗示所有能力都有恢复生命周期。

### 4.4 收窄 `decide()`，但保留应用编排职责

目标结构：

```text
DecisionRequestBuilder.build(frame)
  → ModelContextProjector.prepare(...)
  → LLMApi.chat(...)
  → response parser
  → ensure_deliver
  → DecisionReplayRecorder.record(...)
```

`JournalBackedLlmDecisionMaker` 继续负责按顺序协调这些步骤。不要为了缩短方法而给每三行代码建立新类。

Replay Manifest 建议移动到 `assistant/replay.py`，由有字段校验的构造对象生成当前 `decision-replay-manifest/v1` dict。它仍保存为 Artifact，并通过 `RecordedDecision.artifact_refs` 随 Step 提交。

Context usage 的估算、实际 usage 和 estimator 校准可以保留在编排器中，也可以收进一个 `ContextUsageObserver`；只有当拆分后确实减少调用协议泄漏时才引入，不作为本轮强制抽象。

### 4.5 在注册收口后再减少审批样板

`ControlOperation` 稳定后，再评估一个小型工厂是否值得：

```python
control_operation(
    domain_id=...,
    name=...,
    description=...,
    input_model=...,
    prepare=...,
    execute=...,
)
```

工厂只负责已经完全相同的机械规则：

- proposal request id；
- action 绑定；
- `control_boundary=True`；
- `exclusive_batch=True`；
- proposal spec 与 approval handler 的成组返回。

领域仍负责：

- 前置事实检查；
- 冻结 payload 的具体字段；
- summary 与 risk；
- 并发 revision / hash 条件；
- 批准后的真实操作；
- 可确定领域错误到 `ControlApprovalExecution` 的转换。

不要提前规定一个能同时表达所有 MCP / Skill 操作的泛型生命周期。如果工厂需要大量回调、策略枚举或空实现，保留显式领域代码更清楚。

### 4.6 Artifact 强引用作为独立决策

如果产品要求通过 `Event.artifact_refs` 做完整引用遍历或垃圾回收，应扩展 Tool handler 的终态结果，例如：

```python
@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    value: object
    artifact_refs: tuple[str, ...] = ()
```

Dispatcher 将 `value` 写入 `CommandOutcome`，将 `artifact_refs` 写入同一个 `CommandOutcomeReceived` EventDraft。

这是一项合理的 Runtime 窄接口增强，但不属于 Assistant 组合重构的必要前置。实施前必须单独确认：

- 普通 handler 返回值是否继续兼容，还是一次性严格迁移；
- `ToolTerminal` 与成功结果如何共同携带引用；
- Attempt 失败时已创建 artifact 的清理语义；
- Journal 对 artifact refs 的现有校验是否已经足够。

在作出该决定前，保留 externalized stub 中的 artifact id，不错误地把 handler 产生的引用塞进 `RecordedDecision`。

### 4.7 删除失效装饰器

删除生产未使用的 `DeliveringDecisionMaker` 及只验证它的测试。保留：

- `ensure_deliver`；
- `emit_delivery`；
- `deliver_binding`。

这项删除应与其他结构调整分开提交和验证，避免把行为变化混入清理。

---

## 5. 实施顺序

每阶段独立可验证，不跨阶段保留两套并行生产路径。

### P0：用测试固定事实

在重构前补齐：

- 当前 schema 的名称、顺序和内容快照；
- Toolset / Management catalog instruction 的精确内容与顺序；
- Management 未加载、加载后对 Control schema 的门控；
- ToolSurface / ManagementSurface 从 Journal 恢复后的可见性；
- Control pending 不跨进程恢复；
- 普通非控制 Step 的 `after_committed_step()` 返回 `None`；
- 控制调用 stage 后若 Step 未提交，是否会残留并屏蔽后续控制 schema。

最后一项是调查性测试。若确认存在残留，再依据真实失败路径设计清理；不以 `basis_state_version` 会自行漂移为前提。

### P1：收口 `ControlOperation`

- MCP / Skill composition 输出 operation；
- ControlPlane、ManagementDomain 和名称冲突集合从 operation 派生；
- 校验 proposal request action 与 operation action 一致；
- 删除 assembly 中的平行清单和控制名抄写。

验收：新增一项同域控制操作只需在领域 composition 注册一次，Assistant 不再逐份追加 spec、handler 和 control name。

### P2：引入决策贡献与最终请求

- 增加 `DecisionContribution`、`PreparedDecisionRequest` 和 builder；
- 将 Management + Control 门控封装成一个 contributor；
- `JournalBackedLlmDecisionMaker` 不再持有四种具体能力来源；
- 旧、新请求的 prompt 与 schemas 逐项完全相同。

验收：新增一种真正的模型可见来源只需实现 contributor 并在装配处注册；不修改 DecisionMaker 主链路。

### P3：收口恢复投影

- `resume_session()` 遍历 `SessionProjection`；
- 装配层显式注册 ToolSurface 和 ManagementSurface；
- Control 易失契约保持不变。

验收：新增可恢复 Host 投影只改其实现和装配注册，不修改恢复函数。

### P4：拆出 Replay，删除死代码

- typed Replay Manifest builder / recorder；
- 删除 `DeliveringDecisionMaker`；
- 保持 replay schema、artifact 内容和 Step artifact refs 不变。

### P5：按实测结果减少审批样板

只在 `ControlOperation` 已经证明稳定后提取小型工厂。以重复语义和修改扩散是否下降验收，不预设删除行数。

### 独立议题：工具结果 artifact refs

单独形成 Runtime 窄接口设计与测试，不与 P1–P5 捆绑。若不需要基于 Event metadata 的引用遍历，可以暂不修改。

---

## 6. 防回潮不变量

- **D1｜一次模型请求只有一个最终可见结果。** Prompt、schemas、allowed names 和 control names 必须由同一个 builder 产出。
- **D2｜Management 显式门控 Control。** 控制 schema 不能通过 contributor 顺序、全局状态或调用方约定隐式出现。
- **D3｜一项控制操作只注册一次。** Spec、action、handler、domain 和名称占用必须从同一个 `ControlOperation` 派生。
- **D4｜恢复只重建 Journal 可推导投影。** ToolSurface 与 ManagementSurface 的内存状态可丢弃；恢复不得创造没有事实依据的激活。
- **D5｜控制审批保持易失。** 重启放弃未确认方案，绝不恢复或自动执行。
- **D6｜不同变化原因使用不同窄端口。** DecisionContributor、SessionProjection 和 ControlOperation 不合并成 Capability / Plugin 基类。
- **D7｜MCP 与 Skill 领域独立。** 共享控制协议不意味着共享 Registry、Application Service 或生命周期。
- **D8｜外置 artifact 的引用遵守产生时序。** 决策阶段 artifact 随 Step；工具执行阶段 artifact 只能随 Outcome 或其明确的执行事实。
- **D9｜内部契约错误直接暴露。** 注册漂移、未知 action、重复工具名和无法恢复的 revision 不降级成“能力不可用”。

---

## 7. 验收标准

完成 P1–P5 后应满足：

1. `decision.py` 不再按 ToolSurface / Skill / Management / Control 写具体拼接分支；
2. `assembly.py` 不再维护平行 spec、handler 和 control name 清单；
3. `resume_session()` 不再点名具体投影类型；
4. MCP / Skill 仍可彼此独立删除，不在 Runtime 留下领域名词；
5. 所有渐进加载时序与重构前一致；
6. 相同 Frame 生成的 prompt、tool schemas、允许名称和 replay request 保持等价；
7. 未加载管理域时控制提案不可见，加载成功后的下一 Step 才可见；
8. 重启不会恢复或执行旧控制审批；
9. 新增控制操作和新增决策贡献者都不需要修改已有消费者主链路；
10. 未引入面向未知扩展者的 Plugin / Capability 生命周期。

本轮成功的标志不是文件更少或代码更“统一”，而是同一变化原因集中、真实依赖显式、漏注册能够在装配或测试阶段暴露。
