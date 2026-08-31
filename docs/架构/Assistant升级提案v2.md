# Assistant 升级提案 v2

> 状态（2026-08-31）：提案 v2.1，未开工。本文不构成实现授权。
> 与 v1 的关系：`Assistant升级提案.md` 保留不动。v2 继承它的 §1（必须保持的契约），
> 修订它的问题定位，并大幅削减它的候选抽象。两份文档可对照阅读。
> v2.1 依据一次外部评审修订，逐条裁决见 §9。
> 主要范围：`helperme/assistant/`，以及 `helperme/tools/executor.py` 与
> MCP / Skill 装配的交界。

---

## 0. 结论

本提案的设计中心是**唯一权威装配路径**。当前 Assistant 的主要问题不是缺抽象，而是四件更朴素的事，外加两个行为缺陷：

1. **两处行为缺陷**：一处已实证且跨进程永久（失败的 `load_toolset` 结果让 Session 永远无法恢复），一处在单个进程生命周期内可长期存活（控制面暂存残留后静默隐藏全部控制工具）。
2. **三处死代码**：`tool_schemas` 参数、`AssistantAssembly.model_tools`、`DeliveringDecisionMaker`，全部无生产调用方。
3. **装配分裂**：五个依赖全部 Optional，且"造零件"与"接线"分处两个模块，导致生产、live 测试、压测脚本各自接了一套不同的 Assistant。
4. **机械样板复制**：`externalize_payload` 四份；ToolSpec 执行适配三份，其中两份是对已存在的 `ToolsExecutor._execute_spec` 的重新实现。

只有一处真正需要新的数据结构：控制操作的注册事实分散在三处，名称冲突边界还漏了一处，遗漏无法由构造期校验发现。

本轮以**删除、修复、复用既有抽象**为主，只引入一个新概念（`ControlOperation`），它是"唯一装配路径"这个中心内部唯一必要的新数据结构。

### 0.1 v2 与 v1 的三点判断差异

**根因定位。** v1 认为根因是 `decision.py` 认识具体能力来源，因此要引入贡献者协议。v2 认为根因是所有依赖都是 Optional 且装配分裂；`_schemas()` 里的 `if x is not None` 链是可选性的产物，不是"认识来源"的产物。不先消除可选性，引入贡献者协议只会让分支搬家。

**抽象数量。** v1 引入四组新概念（`PreparedDecisionRequest` / `DecisionContributor` / `ControlOperation` / `SessionProjection`）。v2 按 §3 的门槛筛选后只保留 `ControlOperation`，并指出 management 与 skill 侧真正该做的是复用已经存在的 `ToolsExecutor`，而非新建适配抽象。

**优先级。** v1 的 P0 是补测试固定当前事实。v2 的 P0 是修 A1——它是跨进程永久的功能损坏，且修法会直接影响后续 envelope 决策，不能留到重构之后。

---

## 1. 必须保持的现有契约

完整表述见 v1 §1，此处只记结论，本轮不改动：

- **Runtime 边界**：Event 是唯一持久执行事实。Runtime 不认识 MCP、Skill、管理域或控制审批。
- **渐进式加载**：加载类工具的成功 Outcome 提交后，具体能力从下一 Step 才可见。schema 序列与 instruction 内容不得变化。
- **控制审批易失**：`AssistantControlPlane` 的暂存与待确认只活在进程内。重启放弃未确认方案，绝不自动执行。
- **MCP 与 Skill 领域独立**：不建立共同基类，不合并 Application、Registry 或生命周期。
- **未知异常原样穿透**：`assistant_failure_message()` 无法翻译的异常由 Channel 直接 `raise`，终止进程。已知的领域拒绝才转成结果值。（见 `channels/cli/console.py` 与 `channels/telegram/assistant.py`。）本轮新增的所有语义都必须与这条一致。

---

## 2. 问题清单

按性质分类而非按文件位置，便于按类型决定处理方式。每条标注证据强度：**已实证**（跑过复现）、**代码确定**（阅读即可确认）、**结构性推断**（由代码结构导出，未定位具体触发顺序）。

### A 类：行为缺陷（优先修）

#### A1 失败的 `load_toolset` 结果让 Session 永久无法恢复 —— 已实证

`project_toolset_activations()` 遍历所有 `SUCCEEDED` 的 Outcome，并在检查 `ok` 之前先校验字段集合：

```171:174:helperme/assistant/toolsets.py
        if not isinstance(value, Mapping):
            raise ValueError("load_toolset outcome 必须是 object")
        if set(value) != {"ok", "code", "data"}:
            raise ValueError("load_toolset outcome 字段不匹配")
```

但 `ToolSurface.load()` 业务失败时返回**五个键**（多出 `error` 与 `hint`），且没有抛异常，因此 Dispatcher 把它记为 `SUCCEEDED`：

```186:189:helperme/runtime/dispatcher.py
            outcome = (
                result.outcome
                if isinstance(result, ToolTerminal)
                else CommandOutcome(OutcomeStatus.SUCCEEDED, value=result)
            )
```

后果：模型只要有一次传错 `toolset_id`（很常见的模型错误），该 Session 的 Journal 里就留下一条五键 Outcome，此后每次 `resume_session()` 都抛 `ValueError: load_toolset outcome 字段不匹配`，**该 Session 永久无法恢复**，且错误信息不指向真实原因。这是本清单里唯一跨进程永久的缺陷。

实证方式：脚本化决策请求加载不存在的 toolset，settle 后用新的 `ToolSurface` 调 `resume_session()`。观测到两个 `SUCCEEDED` Outcome（其一为五键失败 dict）与 `RESUME FAILED: ValueError`。

**这是遗漏而非设计**，因为同一文件族的 `project_management_activations()` 有防护，且顺序正确——先跳过失败，再校验字段：

```77:82:helperme/assistant/management.py
        if "ok" not in value or type(value["ok"]) is not bool:
            raise ValueError("load_management_tools outcome ok 无效")
        if value["ok"] is False:
            continue
        if set(value) != {"ok", "code", "data"}:
            raise ValueError("load_management_tools outcome 字段不匹配")
```

两个投影函数的防护不对称，正是"复制时漏了一句"的痕迹。

现有测试没有覆盖到，是因为 `test_unknown_toolset_is_a_model_correctable_error` 直接调用 `surface.load()`，失败结果从未进入 Journal。**缺的测试是"失败结果进入 Journal 之后能否恢复"，而不是"失败结果对模型是否友好"。**

#### A2 控制面暂存残留在单进程内静默隐藏全部控制工具 —— 代码确定（触发条件明确，未实证）

暂存按四元组 key 写入，但可见性按 session 判断：

```86:92:helperme/assistant/control.py
        if (
            session_id in self._active_sessions
            or session_id in self._pending
            or any(key.session_id == session_id for key in self._staged)
        ):
            return []
```

`stage()` 写入 `_DecisionKey(session_id, trigger_event_id, decision_cursor, basis_state_version)`，只有 `after_committed_step()` 用完全相同的四元组才 `pop`。pop 的条件严格强于 stage 的条件，因此存在无法回收的路径。

**风险窗口需要精确界定，不能笼统称"永久"。** 关键在于 `stage()` 发生在 `decide()` 内部，而 `decide()` 运行在 `advance()` 创建的、可被取消的 task 里，此时 Step 尚未提交。`advance()` 对失败分两类处理：

```274:279:helperme/runtime/runtime.py
            except LeaseLostError:
                await self._journal.release_step(lease)
                return AdvanceResult(None, RuntimeStatus.RUNNABLE)
            except BaseException:
                await self._journal.release_step(lease)
                raise
```

- **重抛路径**（含 replay artifact 写入失败等任意异常）：异常穿透到 `SessionScheduler._record_failure()`，Channel 端 `raise error` 终止进程。这类残留不跨进程存活，因此不构成永久缺陷。
- **`LeaseLostError` 路径**：Runtime 显式视其为可恢复，返回 `RUNNABLE` 且**不抛异常，进程继续运行**；而 operation task 在 `finally` 中被取消，`stage()` 已写入的内存副作用留下。

所以准确定位是：**残留不跨进程，但在 `LeaseLostError` 之后可在单个进程生命周期内长期存活，静默隐藏该 Session 的全部控制工具，无任何错误提示。** 优先级低于 A1（A1 由常见模型错误即触发且跨进程永久；A2 需要 lease 丢失且当轮恰好是控制调用）。

**根源观察（比修 key 结构更值得记录）：** `stage()` 是一次发生在 Step 提交之前、且位于可取消 task 内部的 Host 内存写入。参数校验必须在决策期完成（失败要转成 `InvalidLLMResponse` 让模型重试），但"记住这次控制调用"本可以延后到提交之后。当前之所以用内存暂存传递，是因为控制调用不产生任何 Command，Step 里没有可供 `after_committed_step()` 识别的痕迹——而让 Step 携带该痕迹会使 Runtime 认识控制概念，违反 §1。因此本轮仍按 §4.1 的方式收紧清理，但把"决策期 Host 副作用"记入 §7 Q3 作为后续议题。

#### A3 `resolve()` 的异常路径同样导致静默隐藏 —— 代码确定

```174:177:helperme/assistant/control.py
        execution = await self._handlers[request.action].execute(
            request.payload,
        )
        del self._pending[session_id]
```

`execute()` 抛异常时 `del` 不执行，`_pending` 留下条目，而 A2 引用的 `schemas()` 同样检查 `_pending`，于是控制工具被隐藏。v1 完全没有提到这条。

语义已由既有原则确定，**不是产品偏好选择**：控制执行可能已经产生部分外部副作用（例如 MCP 安装已写入 Registry，连接测试阶段才抛异常），系统无法确认执行是否成功，因此不能允许用户用同一个 pending request 重试。正确语义是：

```text
批准请求只消费一次
→ 执行前移除 pending
→ 未预期异常原样穿透并终止进程（与 §1 末条一致）
→ 不自动重试
```

已知的、可确定的领域拒绝继续由 handler 返回 `ControlApprovalExecution(succeeded=False)`，这一点当前实现已经做到（如 `McpRecoveryPreconditionError` 的处理）。修法是把 `del` 移到 `execute()` 之前。

#### A4 Skill 工具的参数校验错误码偏离既有约定 —— 代码确定

仓库里实际存在两个可辨的错误码族：

- **ToolSpec 参数校验失败**用 `VALIDATION_ERROR`：`tools/executor.py`（两处）、`ManagementToolAdapter._handler`，且有测试锁定（`tests/tools/test_command_execution.py`）。
- **手写的非 ToolSpec 参数检查**用 `INVALID_ARGUMENT`：`ToolSurface.load` 的 `toolset_id` 检查、`artifacts.py`（三处）、`ManagementSurface.load` 的 `domain` 检查。

`SkillToolAdapter._handler` 做的是 ToolSpec 校验，却返回 `INVALID_ARGUMENT`，是唯一偏离者。修法因此是确定的——改为 `VALIDATION_ERROR`，不需要重新选约定。

### B 类：死代码（纯删除）

#### B1 `tool_schemas` 参数没有任何调用方 —— 代码确定

全仓搜索 `tool_schemas` 只命中 `decision.py` 内部三行。生产链（`bootstrap.py`）、live 测试、压测脚本都传 `surface=`，因此 `_schemas()` 的 else 分支是死路：

```186:188:helperme/assistant/decision.py
        else:
            schemas = list(self._tool_schemas)
```

#### B2 `AssistantAssembly.model_tools` 被赋值但从未被读取 —— 代码确定

```138:138:helperme/assistant/assembly.py
    model_tools = [*builtin_tools.schemas, READ_ARTIFACT_SCHEMA]
```

它与传给 `ToolSurface` 的 `base_schemas` 内容重复，是 B1 那个死参数的供货方。两者同生共死。

#### B3 `DeliveringDecisionMaker` 未进生产且契约已分叉 —— 代码确定

沿用 v1 §2.7 的结论。补充：它声明返回 `ModelDecision`，而生产 DecisionMaker 返回 `RecordedDecision`，所以它已经不能透明包裹当前实现，不是"备用接线点"。保留 `ensure_deliver` / `emit_delivery` / `deliver_binding`。

### C 类：装配分裂（v2 认定的根因）

#### C1 五个 Optional 依赖，生产只用一种配置 —— 代码确定

`JournalBackedLlmDecisionMaker.__init__` 有 `tool_schemas` / `surface` / `skill_tools` / `control` / `management` 五个可选依赖，名义上 32 种配置，生产只用 1 种。`_schemas()` 的分支链、`decide()` 里的 `if catalog is not None` / `if self._management is not None`、`_decision_from_response()` 里的 `if self._control is None: raise RuntimeError("control call accepted without control plane")`——全部是这个可选性的直接代价。最后那句 `RuntimeError` 尤其说明问题：它防的是一个**类型上允许、语义上不可能**的状态。

#### C2 装配与接线分处两处，产生三套不同的 Assistant —— 代码确定

`build_assistant_assembly()` 只造零件；真正接线成 DecisionMaker / Scheduler / Sessions 发生在 `bootstrap.py`。后果是：

| 接线点 | surface | skill_tools | control | management |
| --- | --- | --- | --- | --- |
| `helperme/bootstrap.py` | 有 | 有 | 有 | 有 |
| `tests/live/test_runtime_live.py` | 有 | 有 | 无 | 无 |
| `tests/benchmarks/final_session_stress_live.py` | 有 | 有 | 无 | 无 |

同一个生产对象有三种接法，没有一种是权威。live 测试和压测因此**从未覆盖生产的真实模型可见面**——它们看不到管理目录说明，也不会遇到控制工具。这是"实现混乱"最直接的来源，比 `if` 链严重。

#### C3 Optional 沿调用链继续传染 —— 代码确定

`AssistantSessions` 与 `resume_session()` 都把 `control` / `management` 声明为可选，于是每个消费点都要再写一次 `if x is not None`。`AssistantSessions.resolve_control()` 里 `raise ValueError("Assistant 未装配对话控制面")` 同样在防一个生产中不可能的状态。

### D 类：机械样板复制

#### D1 `externalize_payload` 样板四份 —— 代码确定

`decision._executor_handler`、`toolsets._loaded_handler`、`ManagementToolAdapter._handler`、`SkillToolAdapter._handler` 都写着同一段：

```python
payload, _artifact_id = externalize_payload(
    result,
    gateway.for_session(context.session_id),
    max_chars=settings.size_externalize_chars,
    preview_chars=settings.preview_chars,
)
```

v1 §2.6 只把它当作"`artifact_id` 被丢弃"的证据，没有把这四份复制本身列为待收口项。收成一个包装函数后，v1 §4.6 那个 Runtime 窄接口增强会从改四处变成改一处——**所以这一步是那个独立议题的低成本前置，而不该排在它之后**。

#### D2 ToolSpec 执行适配三份，但本轮只能改错误码 —— 代码确定

`ToolsExecutor._execute_spec()` 已经是通用的"校验参数 → 失败返回错误结果 → 调 handler → 规范化返回值"适配器：

```114:134:helperme/tools/executor.py
    @staticmethod
    async def _execute_spec(
        spec: ToolSpec,
        payload: object,
    ) -> dict[str, Any] | ControlApprovalRequest:
        try:
            data = spec.parameters.validate(payload)
        except ToolArgumentsError as exc:
            return normalize_tool_result(
                {
                    "ok": False,
                    "code": "VALIDATION_ERROR",
                    "error": exc.details,
                    "hint": "按工具 schema 修正参数后重试。",
                }
            )

        result = await spec.handler(data)
        if isinstance(result, ControlApprovalRequest):
            return result
        return normalize_tool_result(result)
```

`ManagementToolAdapter._handler` 与 `SkillToolAdapter._handler` 各自重新实现了一遍，且都跳过了 `normalize_tool_result()`。所以内置工具的 envelope 由代码保证恰好是五键；管理诊断工具和 Skill 工具的 envelope 靠各 handler 手写维护（`mcp/management_tools.py`、`skills/runtime.py` 都是手写 dict）。

**但"复用参数校验而不改成功结果形状"是做不到的**：`_execute_spec` 的成功路径必然经过 `normalize_tool_result()`，两者在同一个方法里，无法只取一半。可选方向只有三个：

1. 本轮只修 A4 的错误码，保留几行显式校验；
2. 从 `ToolsExecutor` 提取一个极小的参数校验函数供三处复用；
3. 明确决定统一结果 envelope，再整体复用 Executor。

**不要给 `ToolsExecutor` 加 `normalize=False` 之类的策略开关**——那会让一个本来清晰的既有抽象变得含混。

方向 2 看起来最划算，但它有一个隐含成本：两边的**错误载荷结构本来就不同**。`executor` 把 `exc.details` 放进 `error` 并由 `_as_str_error()` 字符串化；`ManagementToolAdapter` 把它放进 `data.details`，`error` 是一句固定英文。提取共享校验必须选定一种载荷形状，而那已经是模型可见的协议行为变更。

**本轮取方向 1**：只改 `SkillToolAdapter` 那一个错误码字符串。把"提取共享校验 + 统一错误载荷"整体归入 §7 Q2 的 envelope 议题。

#### D3 构造期不可能的重复检查占据热路径 —— 代码确定

```45:48:helperme/assistant/skills.py
            catalog_specs = self._catalog.tool_specs()
            specs = {spec.name: spec for spec in catalog_specs}
            if len(specs) != len(catalog_specs):
                raise ValueError("Skill tool catalog 包含重复 name")
```

`SkillToolCatalog.tool_specs()` 要么返回 `[]`（没有启用的 Skill），要么返回 `load_skill` 与 `read_skill_resource` 两个固定名称的 spec——名称重复在构造上不可能。因此**直接删除这段检查**，不需要把它"移动"到 Catalog。catalog 内容随启用状态变化，所以"每次调用都重新取 specs"应保留。

### E 类：注册事实分散（唯一需要新数据结构）

#### E1 注册事实抄了三处，名称冲突边界还漏了一处 —— 代码确定

七项 MCP / Skill 控制操作当前分别进入三个集合：`AssistantControlPlane.specs`、`AssistantControlPlane.handlers`、`ManagementDomain.control_names`。`assembly.py` 第 73–118 行几乎全是这份手抄清单。

**第四处是缺口而非重复**：控制名称并没有进入 `ToolSurface.reserved_names`。`ManagementSurface.names()` 只返回加载器与诊断工具名：

```185:186:helperme/assistant/management.py
    def names(self) -> tuple[str, ...]:
        return (LOAD_MANAGEMENT_TOOLS, *self._adapter.names())
```

`_adapter` 由各域的 `diagnostic_specs` 构造，不含 `control_names`。所以七个 proposal 工具名当前不在 ToolSurface 的冲突集合里。

这个缺口目前**不可触发**：MCP 工具名一律经 `encode_tool_name()` 编码为 `mcp__{server_id}__{tool}` 形式，不可能等于 `propose_mcp_install`。也就是说，名称安全当前依赖 `helperme/mcp/adapter.py` 里的一个字符串前缀约定，而不是依赖 reserved 集合。一旦新增非 MCP 的 `ToolsetProvider`，这层保护就消失，而失效表现是加载管理域后 `_tool_names()` 抛重复名错误、进程终止。

因此它归入 E1 一起修（从 `ControlOperation` 派生 reserved 名称，成本为零），不单列为 A 类缺陷。

**漏一处的后果是静默的**，这是 `ControlOperation` 唯一的立项理由：

- spec 进了 ControlPlane 但漏了 `ManagementDomain.control_names` → 模型永远看不见这个工具，没有任何地方报错；
- proposal handler 里硬编码的 `action=` 与 handler 类的 `action` 属性不一致 → 直到 Step 提交后 `after_committed_step()` 才抛 `KeyError`。

需要澄清一处易被误解的措辞：spec tuple 与 handler tuple **不按位置配对**，`AssistantControlPlane` 分别按 `spec.name` 和 `handler.action` 建表，因此调整 tuple 顺序不会造成错位。真正无法在装配期验证的，只有"proposal 返回的 `action` 与某个 handler 的 `action` 相等"这一条关系——它当前只在运行期暴露。

### F 类：已知但本轮不动

- **F1 `ToolSurface` 的 owner 表是全局的，loaded 表是 per-session。** `runtime.bind_tool()` 也是全局的，所以 Session A 加载的工具 Binding 对 Session B 存在，只是 schema 不可见；`reset(session_id)` 只清 `_loaded` 不清 `_tool_owners`。当前不可利用，因为 `_invoke_requests()` 用本轮 schema 派生的 `allowed_tool_names` 拦住了越界调用。记录，不改。
- **F2 工具结果 artifact 的强引用通道。** 沿用 v1 §2.6 / §4.6 的定位与时序分析：需要改 Runtime 的 Tool handler / Dispatcher 窄接口，是独立议题。前置改为先做 D1。
- **F3 `mcp/approval.py` 与 `skills/approval.py` 的工厂化——本轮明确否决。** v1 §2.5 称两者"反复实现同一机械流程"。逐个比对七项操作后，v2 认为这条被高估：真正逐字重复的只有 `id=f"approval-{uuid4().hex}"`、`control_boundary=True`、`exclusive_batch=True`。payload 字段校验、summary / risk 文案、前置事实检查、revision / hash 条件各不相同，七项操作里没有两项的 payload 形状相同。**降级为：可提取的只有 request id 生成，收益不足以单独立项。**
- **F4 `decide()` 的职责数量。** v1 §2.4 列了八项职责。v2 同意 replay manifest 值得独立命名（它是有 schema 版本号的对外产物），但不同意为此重排整条链路。做完 B、C 两类后 `decide()` 会自然缩短，届时再看。

---

## 3. 本轮新增结构抽象的门槛

**必要条件：存在必须共同变化的注册事实，且遗漏不能由现有构造期校验可靠发现。**

这是本轮的严格门槛，不是普遍唯一的抽象判据。共享不变量、同一变更导致多处同步，即使失败是响亮的，长期也可能值得抽象；但本轮不以此立项，因为当前的痛点集中在"沉默的漏注册"，而每个新概念都要长期偿还理解成本。

**明确不构成本轮立项依据：** `if` 分支数量；文件、类或函数数量；"看着不整齐"、"职责不单一"；为尚未出现的扩展者预留接口（Automation、SubAgent、Long Memory 都还不能确定形状）；消除一个只有两三个实现且不会增长的枚举。

按此门槛筛 v1 的四个候选：

| v1 候选 | 是否满足门槛 | v2 结论 |
| --- | --- | --- |
| `ControlOperation` | 满足。三处注册 + 一处缺口，且 action 一致性无法装配期验证 | **采纳** |
| `PreparedDecisionRequest` | 不满足。schema 与 instruction 漏接会立刻表现为模型看不到工具，可被快照测试可靠发现 | 降级为一个合并函数 |
| `DecisionContributor` | 不满足。新增来源本来就要改装配处一行 | 不做 |
| `SessionProjection` | 不满足。引入后仍需记得在装配处注册 | 不做，改为消除 Optional |

对 `SessionProjection` 补一句依据：`resume_session()` 里那两行 `await x.rehydrate(session_id, events)` 已经是最短表达。**搬家不是消除**——把"调用方要记得加一行"换成"装配方要记得加一行"，遗漏风险没有下降，概念数量却上升了。

---

## 4. 方案

### 4.1 修 A 类缺陷

**A1**：把 `project_toolset_activations()` 的校验顺序改成与 `project_management_activations()` 一致——先确认 `ok` 是 bool，`ok is False` 时 `continue`，之后才校验字段集合。同时给两个投影函数加对称测试。对已损坏的 Journal 自动生效，不需要数据迁移。

**A2**：`_staged` 改为按 `session_id` 索引，每个 Session 最多一条暂存，新暂存覆盖旧暂存；`after_committed_step()` 仍按四元组匹配决定是否执行，但无论是否匹配都清掉该 Session 的暂存。这样未提交的暂存最多存活到该 Session 的下一个 Step。

该设计对所有未匹配路径一致生效，因此不需要先定位触发顺序。需要覆盖的测试路径有四条：

1. 正常的非控制 Step（`after_committed_step()` 返回 `None`，且不影响后续控制可见性）；
2. DecisionMaker 在 `stage()` 之后失败（重抛路径）；
3. Step commit 失败；
4. `LeaseLostError` 路径——进程不终止，残留必须在下一个 Step 被清除。

第 4 条是 A2 唯一能在单进程内长期存活的路径，不能省略。

**A3**：把 `del self._pending[session_id]` 移到 `execute()` 调用之前，实现"批准请求只消费一次"。未预期异常继续原样穿透。

**A4**：`SkillToolAdapter` 的参数校验错误码改为 `VALIDATION_ERROR`。

### 4.2 删 B 类死代码

删除 `tool_schemas` 参数及其 else 分支、`AssistantAssembly.model_tools`、`DeliveringDecisionMaker` 及只验证它的测试。三项都是纯删除。`DeliveringDecisionMaker` 的删除**单独提交**，避免把行为变化混入清理。

### 4.3 收口 C 类装配

`surface` / `skill_tools` / `control` / `management` 在 `JournalBackedLlmDecisionMaker`、`AssistantSessions`、`resume_session()` 中一律改为必填。随之删除三处防守不可能状态的抛错（`RuntimeError("control call accepted without control plane")`、`ValueError("Assistant 未装配对话控制面")`，以及 `_schemas()` 与 `decide()` 里的可选分支）。

把接线从 `bootstrap.py` 移入装配层，让它产出一个可直接运行的对象（含 DecisionMaker、Scheduler、Sessions、已 attach 的 surface）。`bootstrap.py` 只负责配置、生命周期与 Channel 交界。**live 测试与压测脚本改为走同一个入口**，从而真正覆盖生产的模型可见面。

`assembly.py` 里 `mcp: object` / `skills: object` 顺带补上真实类型（`McpAssembly` / `SkillAssembly`）。

预期：做完这一步，`_schemas()` 从五个分支降到线性拼接，v1 §4.1 想解决的现象消失一半，而没有引入任何新概念。

### 4.4 合并 D 类样板

- **D1**：抽一个 `externalizing(gateway, settings)` 包装，四处 handler 复用。
- **D2**：只改 `SkillToolAdapter` 的错误码（即 A4）。共享校验函数与 envelope 统一一并留给 Q2。
- **D3**：删除 `SkillToolAdapter._handler` 里构造上不可能的重复检查。

### 4.5 收口 E1：`ControlOperation`

沿用 v1 §4.2 的设计，包括把 handler 协议下沉为只描述 `action + execute` 的窄协议，以及在 proposal 返回时立即校验 `request.action == operation.action`——这正是当前唯一无法装配期验证的关系。

MCP / Skill 的 composition 各自输出自己的 operation tuple；`AssistantControlPlane` 可调用 spec、批准执行表、`ManagementDomain` 的控制可见性、**以及 `ToolSurface` 的名称冲突集合**全部从 operation 派生。最后一项同时补上 E1 描述的缺口，使名称安全不再依赖 `encode_tool_name()` 的前缀约定。`assembly.py` 不再手抄任何控制名。

`_StagedCall` 改为持有 operation 而非 `(spec, input_data)`——与 4.1 的 A2 修改是同一处代码，应合并实施。

### 4.6 本轮不做

v1 的 P2（决策贡献者）、P3（恢复投影协议）、P5（审批工厂）本轮不做，理由见 §3 门槛表与 F3。

`decide()` 里的 replay manifest 可以在 4.3 完成后顺手移入 `assistant/replay.py` 并加字段校验，但这是可选项，不作为验收条件。

---

## 5. 实施顺序

每阶段独立可验证，不跨阶段保留两套并行生产路径。

**P0 修缺陷。** A1（含两个投影函数的对称测试）、A3、A4。A1 优先于一切，因为它跨进程永久。A3 是一行改动加一个测试。

**P1 删死代码。** B1 + B2 一起（同生共死），B3 单独提交。

**P2 收口装配。** C1 → C2 → C3。完成后补一条测试，断言 live / 压测入口与生产入口产出相同的工具名集合——这是防止 C2 再次分裂的唯一有效手段。

**P3 收口控制注册。** E1 + A2。两者改同一处代码结构，合并实施；A2 的四条测试路径在此阶段补齐。

**P4 合并样板。** D1 → D3。D1 完成后重新评估 F2 的成本。

顺序理由：P0 里 A1 是跨进程永久损坏；P1 让后续每一步要读的代码变少；P2 消除可选性，是 P3 的前提（operation 派生要求装配层唯一）；A2 从 P0 移到 P3，因为它与 `_StagedCall` 结构变化耦合，且风险窗口限于单进程内的 `LeaseLostError` 路径。

---

## 6. 不变量

保留 v1 中仍然有效的部分，删去为已砍抽象服务的条目，并新增由 A1、A3、D2 得出的三条。

- **D1｜渐进加载时序不变。** 加载类工具的成功 Outcome 提交后，能力从下一 Step 才可见。重构前后 schema 序列与 instruction 内容逐项相同。
- **D2｜Management 显式门控 Control。** 控制 schema 不能通过遍历顺序、全局状态或调用方约定隐式出现。
- **D3｜一项控制操作只注册一次。** Spec、action、handler、domain 和名称占用必须从同一个 `ControlOperation` 派生。名称冲突边界不得依赖其他模块的命名前缀约定。
- **D4｜恢复只重建 Journal 可推导的投影。** 恢复不得创造没有事实依据的激活。
- **D5｜控制审批保持易失，且批准只消费一次。** 重启放弃未确认方案；执行前移除 pending，不因执行失败而允许重试同一请求。
- **D6｜控制面不得因内部残留而静默降级。** 暂存或待确认状态的清理失败不能表现为"控制工具不存在"。
- **D7｜Journal 投影必须先区分业务成败，再校验成功契约。** 业务失败的 Outcome 不得让恢复流程抛错。
- **D8｜只有被 Host 投影解释的 Outcome 契约变更才是 Journal 重放兼容性变更。** 当前仅 `load_toolset` 与 `load_management_tools` 属此类。其他工具结果对 Runtime 是不透明值，改变其 envelope 属于模型协议行为变更——影响上下文与行为回放，但不会让历史 Journal 无法重建。两类变更的评估标准不同，不得混为一谈。
- **D9｜MCP 与 Skill 领域独立。** 共享控制协议不意味着共享 Registry、Application Service 或生命周期。
- **D10｜内部契约错误直接暴露。** 注册漂移、未知 action、重复工具名不降级成"能力不可用"。未知异常原样穿透并终止进程。
- **D11｜新增结构抽象须通过 §3 门槛。** 不以分支数、文件数或整齐度作为本轮立项依据。

---

## 7. 开放问题（需人裁决）

**Q1｜是否统一工具结果 envelope，以及错误载荷形状？**

现状：内置工具走 `normalize_tool_result()`（五键固定）；管理与 Skill 工具手写 dict，形状不由代码保证。两族的参数校验错误载荷结构也不同（`error` 字符串化 vs `data.details`）。

按 D8，这属于**模型协议行为变更**，不是 Journal 重放兼容性变更，因此代价比 v2.0 估计的低。但它会改变模型看到的错误结构与历史 replay 的语义，需要单独设计。需要决定：接受长期两族并存，还是排一次统一（届时可整体复用 `ToolsExecutor`，并顺带完成 D2 的方向 2 或 3）。

**Q2｜`stage()` 是否应停止在决策期写入 Host 内存？**

A2 的根源是 Host 内存副作用发生在 Step 提交之前，且位于可取消的 task 内。§4.1 的清理策略能消除后果，但没有消除结构。彻底的做法需要让"这次 Step 含一个控制调用"这条事实在提交后可被识别，而不依赖决策期的内存写入——难点在于控制调用不产生 Command，而让 Step 携带该痕迹会使 Runtime 认识控制概念，违反 §1。

建议在 P3 完成后重新评估，不在本轮设计。

**Q3｜A2 修复后，暂存覆盖是否需要通知用户？**

若同一 Session 连续两次 stage（第二次覆盖第一次），用户可能已经看到过第一个方案的确认信息。当前设计下这不会发生（stage 后 `schemas()` 立即屏蔽控制工具），但覆盖语义使它在理论上可能。建议先加断言暴露，而不是先写通知。

---

## 8. 验收标准

完成 P0–P4 后应满足：

1. 业务失败的 `load_toolset` 与 `load_management_tools` Outcome 都不影响 Session 恢复，且两个投影函数有对称测试；
2. 控制面的暂存或待确认残留不会隐藏控制工具，四条路径（正常非控制 Step、DecisionMaker 失败、Step commit 失败、`LeaseLostError`）均有测试；
3. 批准请求只消费一次，执行失败不保留可重试的 pending；
4. `decision.py`、`sessions.py`、`runner.py` 中不存在 `if <capability> is not None` 分支，也不存在防守不可能状态的抛错；
5. 生产、live 测试、压测走同一装配入口，并有测试断言三者的模型可见工具名集合相同；
6. `assembly.py` 不再维护平行的 spec、handler 与控制名清单；新增一项控制操作只改领域 composition；
7. 控制工具名进入 `ToolSurface` 的冲突集合，且该集合从 `ControlOperation` 派生，不依赖 `encode_tool_name()` 的前缀约定；
8. `externalize_payload` 只出现在一处；ToolSpec 参数校验错误码全仓一致；
9. 全仓不存在无调用方的 Assistant 参数、字段或类；
10. 重构前后，相同 Frame 生成的 prompt、tool schemas、允许名称与 replay request 逐项等价；渐进加载时序不变；重启不恢复也不执行旧控制审批；
11. 未引入 Plugin / Capability / Contributor 生命周期抽象。

本轮成功的标志是：**沉默的失败变成响亮的失败，重复的事实收成单一来源，而概念总数没有增加。**

---

## 9. v2.1 修订记录

针对一次外部评审的逐条裁决。

**采纳｜E1 的事实错误。** v2.0 写控制名称"经 `management.names()` 间接进入 `reserved_names`"，事实不成立——`ManagementSurface.names()` 只含加载器与诊断工具名。已改写为"注册事实抄了三处，名称冲突边界还漏了一处"。

**部分采纳｜该缺口的后果。** 评审称"动态 MCP 工具可以与 proposal 工具重名"。核实后不成立：MCP 工具名一律经 `encode_tool_name()` 编码为 `mcp__{server_id}__{tool}`，不可能等于 `propose_mcp_install`。因此这是**结构缺口而非可触发缺陷**——名称安全当前依赖另一模块的字符串前缀约定。归 E1 一起修，不进 A 类。§4.5 与验收标准 7 记录了这一点。

**采纳｜"两个 tuple 静默错位"的措辞错误。** v2.0 一边引用"不按位置配对"的澄清，一边又称 tuple 会静默错位，自相矛盾。已改为：唯一无法装配期验证的是 proposal 返回的 `action` 与 handler `action` 的相等关系。

**部分采纳｜A2 的严重性。** 评审指出"跨进程永久"说重了，正确：未知异常经 `_record_failure` → Channel `raise` 终止进程，残留不跨进程。但评审的"只有错误地继续使用进程才会发生"低估了风险：`runtime.py` 274–276 的 `except LeaseLostError` 是 Runtime 显式的可恢复路径，**不抛异常、进程继续运行**，而 `stage()` 的内存副作用已经留下。已重新定位为"单进程内可长期存活"，优先级从 P0 降到 P3，并把 `LeaseLostError` 加为第四条必测路径。同时新增"决策期 Host 副作用"这一根源观察（Q2）。

**采纳｜A3 不应作为产品偏好保留。** 控制执行可能已产生部分外部副作用，系统无法确认是否成功，因此不能允许重试同一 pending。已按"批准请求只消费一次 → 执行前移除 pending → 未预期异常穿透 → 不自动重试"定稿，原 Q1 删除。§1 相应补入"未知异常原样穿透"这条既有契约。

**采纳｜D2 的方案不可实现。** `_execute_spec` 的成功路径必然经过 `normalize_tool_result()`，"复用校验而不改结果形状"做不到。已改为本轮只修错误码（方向 1），并明确否决 `normalize=False` 式的策略开关。补充了评审未提到的一点：方向 2 也非零成本，因为两边的错误载荷结构本就不同（`error` 字符串化 vs `data.details`），提取共享校验必须先选定载荷形状。

**采纳｜D3 可以更简单。** `SkillToolCatalog.tool_specs()` 只可能返回空列表或两个固定名称，重复在构造上不可能。已改为直接删除热路径检查，不再"移动"到 Catalog。

**采纳｜Q2 混了两种兼容性。** 已按评审表述收窄 D8：只有被 Host 投影解释的 Outcome（`load_toolset`、`load_management_tools`）属 Journal 重放兼容性；其他工具结果的 envelope 变化属模型协议行为变更。这也降低了原 Q2 的估计代价。

**采纳｜判据过于绝对。** §3 已从"唯一判据"改为"本轮门槛"，表述为"存在必须共同变化的注册事实，且遗漏不能由现有构造期校验可靠发现"，并明确共享不变量等理由长期仍可能成立，只是不在本轮立项。
