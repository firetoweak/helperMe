# Phase 6C · Web Search 与 Web Fetch 设计草稿

> 状态：架构修订完成，具体 Provider 与实现细节待定  
> 目标顺序：先回补通用 Run 期能力边界，再完成 `web_search`，随后完成 `web_fetch`，最后进行真实 search → fetch → answer benchmark。

## 1. 本阶段要解决的问题

Phase 6C 回答两个相邻问题：

1. HelperMe 如何让 Agent 发现公开 Web 的候选来源并读取静态页面正文；
2. Plugin 如何在不污染 Core 的前提下，获得当前 Run 所需的只读事实、Artifact 写入能力和私有临时状态。

本阶段包含：

- `web_search`：根据查询发现候选页面，返回有序的结构化结果；
- `web_fetch`：读取给定的公开 URL，返回静态页面的可读正文；
- URL、请求时间和 Provider 身份等可核验事实；
- URL 来源的审计记录，但不把来源记录当成网络授权表；
- URL 规范化、Secret 检查、SSRF 防护和逐跳重定向检查；
- 大正文进入现有 Runtime Artifact，Conversation 保留有界语义和引用；
- 通用 `RunScope`、`ToolFactReader` 和 Run 期能力贡献边界；
- 单一 Search Provider 与单一 Fetch Provider 的真实闭环。

本阶段不包含：

- Browser Automation、登录态、点击、表单和页面脚本执行；
- 站点级 Crawl；
- 多 Provider Registry、运行时切换和自动 fallback；
- 自动凭证探测；
- LLM 对抓取正文的二次摘要；
- Web 专属 Session 状态、事实存储或 Artifact 生命周期；
- 基于 URL 来源的复杂网络访问授权；
- 通用 Plugin 基类、自动发现、依赖解析或版本协商平台。

## 2. 外部参考与取舍

### 2.1 Hermes：Search / Fetch 边界参考

参考：

- [Hermes Web Search 与 Extract 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/web-search.md)
- [Hermes Web 工具入口](https://github.com/NousResearch/hermes-agent/blob/main/tools/web_tools.py)
- [Hermes WebSearchProvider](https://github.com/NousResearch/hermes-agent/blob/main/agent/web_search_provider.py)
- [Hermes Web Provider Registry](https://github.com/NousResearch/hermes-agent/blob/main/agent/web_search_registry.py)

吸收：

- Search 只负责发现候选来源，Fetch 才读取正文；
- 模型工具契约不随具体 Provider 改变；
- URL 安全、输出预算与正文外置不散落到每个厂商适配器；
- Provider 只负责外部调用与响应归一化；
- 大正文确定性外置，不额外调用 LLM。

不吸收：

- 多 Provider 自动选择和 fallback；
- Search 与 Fetch 共用带能力标志的大接口；
- 同步/异步双轨兼容；
- 凭证自动探测和历史兼容出口；
- 把外部失败、内部缺陷和兼容逻辑堆进单一 `web_tools.py`。

### 2.2 OpenCode、nanobot 与 DeerFlow：能力范围对照

这些项目共同说明：

- `web_search`、`web_fetch` 与 Browser Automation 是三种不同能力；
- Search + Fetch 已足以形成最小公开信息获取闭环；
- Browser 应在真实任务需要登录、点击、表单或 JS 交互时再独立引入；
- Search 返回的摘要只是候选线索，不能自动等同于网页正文或最终事实。

DeerFlow 使用“只读取用户提供或先前发现 URL”的约束有助于限制模型随意访问目标，但它同时引入跨 Run 来源恢复、链接继承和访问授权语义。HelperMe 当前只保留 URL 来源审计，不在 MVP 中建立这张授权表。

### 2.3 DeepSeek Harness：能力接入边界参考

DeepSeek Harness 的“能力契约 → Provider → 模型工具”有助于区分：

- Application / Plugin 生命周期拥有配置、凭证和 Client；
- Run 期只获得当前任务需要的能力贡献；
- Provider 是领域端口的实现，不等于 Plugin 本身；
- 模型只看到稳定工具和指令，不认识具体 Provider。

HelperMe 不采用“Everything is a Plugin”。Agent Loop、Session、Conversation、Context、Tool 执行和 Artifact 治理仍属于稳定 Core；本阶段只提炼已经被 MCP、Web 和后续 Skill 共同证明的 Run 期能力边界。

## 3. 已确认的核心原则

1. **Core 不认识 Web**：Core 只认识通用能力描述、ToolSpec、结构化工具事实、RunEvidence、Conversation 和 Artifact。
2. **Plugin 与 Run 能力分层**：Plugin 保存 Application 级配置和资源；Run 只加载当前任务所需的能力贡献和私有临时状态。
3. **结构化事实先于协议文本**：工具执行事实以结构化记录保存，再投影为模型协议中的 Tool Message；Plugin 不解析展示字符串恢复领域事实。
4. **单一事实链，不建立 WebFactStore**：Conversation 保存持久工具事实，RunEvidence 保存当前 Run 的核验证据，Artifact 承载大正文；它们是同一执行链的不同投影和载体，不再建立 Web 专属事实系统。
5. **来源审计不等于访问授权**：`UrlProvenanceIndex` 只回答 URL 从何而来；真正的网络安全由 URL Safety、Secret 检查和传输层逐跳防护承担。
6. **接口隔离**：Search Provider 与 Fetch Provider 是两个端口，不用能力标志表达缺失实现。
7. **严格异步**：Provider 端口只接受 async 实现，不在运行期判断同步/异步并自动转线程。
8. **显式装配**：Provider、配置、凭证和 Client 在 Composition 阶段确定；缺失配置明确失败，不自动探测或切换实现。
9. **正文领域外置先于通用硬上限**：Web 能力主动把大正文写入 Artifact 并保留有界语义；Core 通用 Externalizer 仍是最后一道硬上限。
10. **外部错误稳定返回，内部缺陷直接失败**：只转换已经定义的模型输入、网络和厂商边界错误；契约破坏、生命周期错误和 Artifact 写入失败保留原始异常。

## 4. 需要回补的通用 Run 期能力边界

### 4.1 当前缺口

现有 `ToolsetProvider` 只能根据 `toolset_id` 返回 `ToolSpec`。它足以支持 MCP 的动态工具发现，却不能自然表达 Web 所需的：

- 当前 Session 私有的 Artifact 写入端口；
- Conversation 中持久结构化工具事实的只读查询；
- 当前 Run 私有的 URL 来源索引；
- 后续 Skill 只贡献 instructions、不一定贡献 tools 的情况。

若为 Web 单独向 Core 增加参数，会让 Composition 和 Runtime 持续累积 Plugin 特例；若让 Web Provider 捕获完整 Conversation 或全局 ArtifactStore，又会破坏作用域与所有权。

因此，本阶段把“Toolset 返回工具”提升为更窄但更通用的“Capability 在 RunScope 中产生贡献”。

### 4.2 RunScope

候选契约：

```text
RunScope
├─ tool_facts: ToolFactReader
└─ artifacts: ArtifactSink
```

`RunScope` 只暴露 Plugin 已经出现的通用 Run 期端口，不暴露：

- 可修改的 Conversation；
- Session 聚合；
- ContextState；
- Agent Loop；
- Tool Registry；
- 其他 Plugin 的状态。

`ArtifactSink` 指向当前 Session 私有的 Runtime Artifact 抽屉。Plugin 可以保存自己的大内容，但不能决定 Artifact 生命周期、路径或跨 Session 可见性。

第一版不把 Clock、Logger、HTTP Client、SecretStore 等全部塞入 `RunScope`：

- Provider Client 和 Secret 属于 Plugin / Application 生命周期，由 Composition 显式注入；
- 时间可由 Web Application Service 注入一个小 Clock，尚未证明所有 Capability 都需要；
- Observability 继续由 Core 的通用执行链负责。

### 4.3 ToolExecutionFact 与 ToolFactReader

Conversation 需要保存可持久查询的通用结构化工具事实：

```text
ToolExecutionFact
├─ message_id: str
├─ call_id: str
├─ tool_name: str
├─ arguments: JSON object
└─ result: normalized Tool Result
```

模型协议中的：

```json
{
  "role": "tool",
  "tool_call_id": "...",
  "content": "{...json string...}"
}
```

应由 `ToolExecutionFact` 投影产生，而不是反过来把字符串当作事实源。

只读端口保持很小：

```python
class ToolFactReader(Protocol):
    def by_name(self, tool_name: str) -> tuple[ToolExecutionFact, ...]: ...
```

Web Plugin 只查询自己拥有的 `web_search` / `web_fetch` 事实并解析自己的 `data`；Core 不理解 URL 字段。未来 MCP 可以读取加载 receipt，Skill 可以读取自身加载事实，但 Plugin 不能修改历史记录。

持久 Conversation 与当前 Run Evidence 的语义不同：

- Conversation 中的 ToolExecutionFact 是跨 Run 可恢复的协议事实；
- RunEvidence 是当前 Run 的核验快照，可以保留当前执行所需的完整结果；
- ModelContext 是二者的临时预算投影，不能用于重建 Plugin 状态。

### 4.4 CapabilitySource 与 CapabilityContribution

候选契约：

```text
CapabilitySource
├─ descriptors() → CapabilityDescriptor[]
└─ load(capability_id, RunScope) → CapabilityContribution

CapabilityContribution
├─ tools: ToolSpec[]
└─ instructions: str[]
```

映射关系：

| 能力 | Application / Plugin 持有 | Run 加载后贡献 |
| --- | --- | --- |
| MCP | Server Registry、Client Manager、连接 | 动态发现的 ToolSpec |
| Web | Search/Fetch Provider、Client、配置 | Web ToolSpec、WebRunState 闭包 |
| Skill | 安装内容、索引、解析器 | 指令，必要时附带 ToolSpec |

`CapabilityContribution` 是加载后的不可变快照；其中 Tool handler 可以闭包捕获当前 Run 私有状态和 `RunScope` 的受限端口。

Session 能力快照、revision 校验、Run 内渐进加载和下一轮才暴露工具的现有规则继续成立，只是协议名称从 Toolset 语义逐步收敛到 Capability。是否立即重命名现有类型由实现改动面决定，不要求为了术语一次性机械改名。

这一抽象不覆盖 Goal Plugin。Goal 是围绕多个 Run 的应用工作流，不是模型在某个 Run 中按需加载的一组工具或指令。

### 4.5 生命周期

```text
Application / Plugin 生命周期
├─ Provider 配置与 Secret
├─ 可复用异步 Client
└─ Client 进入、退出和关闭

Session 生命周期
├─ Conversation / ToolExecutionFact
├─ Capability Snapshot
└─ Session 私有 Artifact 抽屉

Run 生命周期
├─ RunScope
├─ 已加载 CapabilityContribution
└─ WebRunState / UrlProvenanceIndex
```

Run 结束后释放 Contribution 和 WebRunState；下个 Run 可以通过 ToolFactReader 从完整 Conversation 重新派生必要的只读索引。不得从 ModelContext、Summary 或 Assistant 普通文本重建可信工具事实。

第一版不建立统一 mount/unmount、Effect、依赖图或热替换机制。Application 资源仍由现有异步生命周期管理；Run 期 Contribution 当前没有需要单独关闭的物理资源。

## 5. Web Plugin 总体架构

```text
Application Composition
├─ SearchProvider
├─ FetchProvider
└─ WebCapabilitySource
          │
          │ load("web", RunScope)
          ▼
Web CapabilityContribution
├─ web_search ToolSpec
├─ web_fetch ToolSpec
└─ WebRunState
   └─ UrlProvenanceIndex
          │
          ▼
WebApplicationService
├─ search / fetch use case
├─ URL 来源审计
├─ URL Safety 顺序
├─ 结构化结果投影
└─ 正文 → RunScope.artifacts
          │
          ├──────────────┐
          ▼              ▼
   SearchProvider    FetchProvider
          │              │
          └──── 外部 Search / HTTP 或 Extract 服务

Core 提交链
Tool handler result
  → RunEvidence
  → ToolResultExternalizer
  → ToolExecutionFact
  → Conversation protocol projection
```

职责：

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| `WebCapabilitySource` | 描述 Web 能力；在 RunScope 中创建 Contribution 和工具闭包 | 网络调用、Conversation 修改 |
| `WebApplicationService` | Search/Fetch 用例、安全顺序、来源审计、结果投影、正文外置 | Agent Loop、Session 持久化 |
| `WebRunState` | 当前 Run 的 URL 来源派生索引 | 网络授权、跨 Run 持久化 |
| `SearchProvider` | 调用外部索引并归一化候选结果 | Fetch、Artifact、Tool Result 编码 |
| `FetchProvider` | 获取给定 URL，归一化最终 URL 与正文 | Conversation、Artifact、Context 预算 |
| Core | RunScope、能力加载、工具执行、结构化事实、Evidence、Artifact 和最终硬上限 | URL、Search、Fetch 领域语义 |

`WebApplicationService` 是职责边界，不要求第一版建立大型 Service 类；两个 handler 配合少量私有函数能够保持边界时，不为形式额外造层。

## 6. Web Plugin 内部契约

以下类型属于 Web Plugin，不进入 Core。

### 6.1 SearchRequest

```text
SearchRequest
├─ query: str
└─ max_results: int
```

规则：

- `query` 在 ToolParameters 边界校验非空和长度；
- `max_results` 有明确上下限，非法值不使用 Provider 默认值兜底；
- 第一版不加入 domain、time range、language、country 等过滤器。

### 6.2 WebSearchItem / WebSearchResult

```text
WebSearchItem
├─ title: str
├─ url: str
├─ snippet: str
└─ rank: int

WebSearchResult
├─ query: str
├─ searched_at: UTC timestamp
├─ provider: str
└─ items: tuple[WebSearchItem, ...]
```

约束：

- `rank` 由返回顺序确定，第一项为 1；
- Provider Adapter 必须在厂商响应边界处理缺字段、非法 URL 和类型错误；
- Provider 端口返回绝对 URL 和明确内部类型；端口调用方直接相信契约；
- `searched_at` 由应用侧记录，只代表本次调用观察时间，不冒充网页发布时间；
- 空结果是成功事实，不伪装成异常；
- snippet 只是 Provider 返回的候选摘要，不证明网页正文。

Provider 只返回归一化的 `tuple[WebSearchItem, ...]`；query、时间和 Provider 身份由应用侧组装。

### 6.3 FetchRequest / WebFetchDocument

```text
FetchRequest
└─ url: str

WebFetchDocument
├─ requested_url: str
├─ final_url: str
├─ title: str | None
├─ content: str
└─ content_type: str | None
```

应用侧结果：

```text
WebFetchResult
├─ requested_url: str
├─ final_url: str
├─ fetched_at: UTC timestamp
├─ provider: str
├─ provenance: search_result | prior_fetch | direct | redirect
├─ title: str | None
├─ content_type: str | None
├─ content: str | None
├─ preview: str | None
├─ artifact_id: str | None
├─ size_chars: int
└─ truncated: bool
```

`direct` 表示请求 URL 没有匹配到历史结构化 Search/Fetch 事实。它可能来自用户输入，也可能由模型依据公开信息直接给出；MVP 不扫描 User/Assistant 文本猜测更细来源。

字段互斥：

```text
truncated = false → content 是完整正文，artifact_id / preview 为空
truncated = true  → content 为空，preview / artifact_id 存在
```

大正文必须先成功写入 Artifact，才能返回 `truncated=true`。

## 7. UrlProvenanceIndex

### 7.1 定位

```text
UrlProvenance
├─ normalized_url: str
├─ source: search_result | prior_fetch | direct | redirect
└─ evidence_id: message_id | call_id

UrlProvenanceIndex
└─ normalized_url → UrlProvenance[]
```

它是 Run 内派生的审计索引：

- 用于说明本次 URL 是否来自历史 Search/Fetch 事实；
- 用于在结果和 Evidence 中保存来源；
- 不决定 URL 是否允许访问；
- 不作为 Session 持久状态。

因此，未出现在索引中的 URL 只会被标为 `direct`，不会因“未经发现”而拒绝。所有 URL 无论来源都必须通过相同 URL Safety。

### 7.2 派生来源

Run 开始或 Web Capability 加载时，通过 ToolFactReader 读取：

1. `web_search` 成功结果中的候选 URL；
2. `web_fetch` 成功结果中的 requested URL 与 final URL。

本 Run 新产生的 Search/Fetch 结果继续增量加入索引。

不读取：

- Assistant 普通文本；
- Summary；
- 任意其他工具的字符串；
- Artifact 正文中的链接；
- ModelContext 投影。

这些内容仍可促使模型直接调用 `web_fetch`，但 provenance 只会诚实记录为 `direct`。

### 7.3 URL 规范化

来源索引和 Fetch 安全检查使用同一基础规范化函数：

- scheme 与 host 大小写归一；
- IDN 统一表示；
- 去除 fragment；
- 规范默认端口；
- 保留 path 和 query 的实际请求语义。

不得擅自删除普通 query、重排可能有语义的参数或合并不同 path。Secret 检查是独立策略，不通过“规范化”偷偷删除凭证。

## 8. URL Safety 与传输安全

### 8.1 两层边界

```text
输入策略
├─ scheme
├─ URL credentials / Secret-like query
├─ host / port
└─ 明确禁止的地址类别

传输安全
├─ DNS 解析结果检查
├─ 实际连接目标与检查结果一致
└─ 每一次 redirect 先检查、后访问
```

只检查 initial URL 和请求完成后的 final URL 不足以防止 SSRF；最终检查发生时，网络访问已经完成。

### 8.2 Fetch Provider 类型决定安全责任

第一版选择 Fetch Provider 时必须同时确定安全模型：

#### 本地 HTTP Fetch

- 禁止底层 Client 自动无条件跟随重定向；
- 每一跳 Location 必须先规范化、Secret 检查、DNS/地址检查，再发送下一请求；
- DNS 校验与实际连接不能各自独立解析并产生可被 rebinding 利用的时间差；
- Fetch Adapter 可以持有注入的 URL Safety Policy，但该 Policy 仍属于 Web Plugin，不进入 Core。

#### 外部 Fetch / Extract 服务

- 本地只向固定 Provider Endpoint 发请求，目标网页由远端服务读取；
- 本地仍检查模型输入和 Provider 返回的 requested/final URL，以保证工具契约与审计事实；
- 远端服务自身的 SSRF 和重定向防护属于 Provider 的安全能力，必须作为选型条件验证；
- 不能把远端服务访问成功误写成“本机已安全访问该目标”。

MVP 不设计同时兼容两种模式的复杂 transport framework；根据第一版 Provider 选择实现一条完整路径。

## 9. Search 执行流程

```text
模型调用 web_search
  → ToolParameters 校验 query / max_results
  → SearchProvider.search(request)
  → Provider Adapter 处理外部响应边界
  → 应用侧记录 searched_at / provider / rank
  → 生成 WebSearchResult
  → URL 增量加入 UrlProvenanceIndex
  → 返回有界结构化 Tool Result
  → Core 记录 RunEvidence、ToolExecutionFact 和协议投影
```

Search 不做：

- 自动 Fetch；
- 自动 LLM 摘要；
- 对候选摘要宣称正文真实性；
- 隐式尝试第二 Provider；
- 因零结果而抛出错误。

## 10. Fetch 执行流程

```text
模型调用 web_fetch
  → ToolParameters 校验 URL
  → URL 规范化
  → 查询 UrlProvenanceIndex，得到 search_result / prior_fetch / direct
  → 输入 URL 的 scheme / Secret / 地址策略检查
  → FetchProvider.fetch(request)
      └─ 本地 Fetch 时，每个网络跳转都先检查再访问
  → 校验 Provider 返回的 requested_url / final_url 契约
  → 记录 fetched_at / provider / provenance
  → 小正文内联；大正文先写 RunScope.artifacts
  → 生成有界 WebFetchResult
  → requested_url / final_url 增量加入 UrlProvenanceIndex
  → 返回结构化 Tool Result
  → Core 记录 RunEvidence、ToolExecutionFact 和协议投影
```

URL 未出现在历史来源索引中不是错误。SSRF、Secret、禁止 scheme 和不支持的内容类型仍是明确的工具边界错误。

## 11. Provider 端口

```python
class SearchProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def search(
        self,
        request: SearchRequest,
    ) -> tuple[WebSearchItem, ...]: ...


class FetchProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def fetch(
        self,
        request: FetchRequest,
    ) -> WebFetchDocument: ...
```

Provider Adapter 是外部边界，负责：

- 厂商请求和响应格式；
- timeout、DNS、TLS、连接、HTTP 状态和 rate limit 的识别；
- HTML / text 等支持类型的正文提取；
- 把厂商响应转换成内部明确类型；
- 本地 Fetch 模式下逐跳执行已注入的 URL Safety Policy。

Provider 端口调用方直接相信内部类型。若一个已经宣称实现端口的对象返回错误类型或违反 `WebFetchDocument` 契约，视为内部缺陷并保留原始异常。

Provider 不读取全局配置和环境变量；Composition 创建 Provider 时显式注入配置、Secret、Client 和必要安全策略。Provider 不访问 Conversation、RunEvidence、ArtifactSink 或 UrlProvenanceIndex。

第一版不建立共同 `WebProvider` 父接口，也不添加：

```text
supports_search
supports_fetch
is_available
get_setup_schema
```

## 12. Conversation、Evidence 与 Artifact

### 12.1 结构化事实提交

通用提交链应保证：

```text
Tool handler 返回 normalized result
  ├─ 当前 Run：记录 RunEvidence
  ├─ 最终硬上限：ToolResultExternalizer
  └─ 持久事实：记录 ToolExecutionFact
          └─ 投影为 Conversation Tool Message
```

Web Plugin 返回的正文结果必须先经过领域投影，确保 URL、时间、Provider、provenance 和 Artifact 引用本身有界。Web 契约测试应验证正常 Search/Fetch 结果不会触发“整个结果外置”。

Core 通用 Externalizer 仍然保留，防止任何工具返回无界结果。若 Web 的领域投影后仍触发整个结果外置，说明 Web 输出预算契约失效；运行时仍保持真实 Artifact 结果，测试必须暴露并修正该缺陷，不能依赖 preview 反解析恢复 URL。

当前不为了 Web 引入通用 `ToolOutcome(receipt, payload)` 或附件框架。等第二种领域工具也需要声明“必须保留哪些语义、哪些正文可外置”时，再评估是否提炼该契约。

### 12.2 为什么正文由 Web 主动外置

Core 通用 Externalizer 不理解 Web 结果中哪些字段是跨 Run 审计语义。如果完全依赖它，URL、时间和 Provider 也会随整个结果进入 Artifact。

因此：

1. Web Plugin 依据正文预算只外置 `content`；
2. bounded result 始终保留元数据、preview 和 Artifact 引用；
3. Artifact 写入失败时保留原始异常，不返回伪造的 `truncated=true`；
4. Core 继续裁决最终工具结果硬上限。

### 12.3 Evidence 语义

- Search Evidence 证明某 Provider 在某次调用时返回了哪些候选 URL；
- Fetch Evidence 证明请求 URL、Provider 返回的最终 URL、观察时间以及正文或 Artifact 引用；
- provenance 说明 URL 与历史 Web 工具事实的关系，不证明网页真实、权威或安全；
- Artifact 是正文载体，不独立证明来源和可信度；
- `searched_at` / `fetched_at` 是观察时间，不是页面发布时间。

## 13. 错误策略

### 13.1 可预期的外部边界错误

转换成稳定 Tool Result：

- 模型参数不符合 ToolParameters；
- URL scheme、credentials、Secret-like query 或目标地址被策略拒绝；
- DNS、连接、TLS、timeout 和远端连接中断；
- HTTP 4xx / 5xx、429 rate limit；
- 页面不存在、不支持的 content type 或正文无法提取；
- Search 成功但没有结果；
- 厂商返回缺字段或非法字段等可识别的外部协议错误。

Provider Adapter 可以把这些错误转换成 Plugin 内部明确异常或结果；Web handler 只捕获已经声明的预期类型并转换成 Tool Result。

### 13.2 内部契约或基础设施缺陷

保留原始异常并使 Run 失败：

- Provider 实现返回违反内部端口契约的对象；
- Composition 缺失必要 Provider；
- ArtifactSink 保存失败；
- WebFetchResult 违反字段互斥约束；
- Web 有界投影违反自身输出预算；
- ToolExecutionFact 无法按 Core 契约提交；
- Client 生命周期、取消或关闭失败。

不自动换 Provider，不把内部缺陷包装成“网页暂时不可用”，不返回伪成功。

## 14. 建议的最小模块边界

名称可在实现时调整，职责不变：

```text
core/
├─ conversation / tool facts       ToolExecutionFact、ToolFactReader
└─ capabilities                    RunScope、CapabilitySource、Contribution

plugins/web/
├─ contracts.py                    Search/Fetch 输入输出、provenance
├─ providers.py                    SearchProvider / FetchProvider
├─ application.py                  search/fetch 用例与正文投影
├─ url_policy.py                   规范化、Secret、SSRF 策略
├─ capability.py                   Web descriptor、Contribution、ToolSpec
├─ composition.py                  配置和 Application 资源装配
└─ <provider>.py                   第一组具体薄适配
```

不要求每项必须独立成文件；若实现很小可以合并。但不能把 Provider 选择、URL Policy、Artifact、事实查询和工具格式重新堆回一个大型 `web_tools.py`。

## 15. 实现顺序

### 15.1 通用边界回补

1. Conversation 能保存 `ToolExecutionFact` 并投影现有工具协议；
2. ToolFactReader 能按工具名读取持久结构化事实；
3. RunRuntime 在提交工具结果时同时维护 Evidence、Fact 和模型消息；
4. 引入最小 `RunScope`；
5. 让渐进加载从 `tool_specs(id)` 演化为 `load(id, RunScope) → CapabilityContribution`；
6. 用现有 MCP 回归验证：动态工具行为、Session 快照与 Run 内冻结语义不变。

### 15.2 `web_search`

1. 确定第一版 Search Provider；
2. 完成 Search 内部契约与 Adapter；
3. 暴露 `web_search` ToolSpec；
4. 验证结构化事实、Evidence、空结果和输出预算；
5. 用 ToolFactReader 在相邻 Run 中读取历史 Search 事实。

### 15.3 `web_fetch`

1. 确定 Fetch Provider，并同时冻结 SSRF / redirect 责任；
2. 完成 URL Policy 和 UrlProvenanceIndex；
3. 完成静态正文获取与支持的 content type；
4. 通过 RunScope.artifacts 完成正文外置；
5. 验证直接 URL、Search URL、重定向和相邻 Run 来源审计。

### 15.4 真实 Agent Benchmark

```text
用户提出需要最新公开信息的问题
  → Agent 调 web_search
  → 从候选结果选择来源
  → Agent 调 web_fetch
  → 必要时分页读取 Artifact
  → 最终回答引用真实 URL
  → Evidence 能核对 query、时间、URL、Provider 和正文载体
```

## 16. 测试与 Benchmark

### 16.1 通用能力边界测试

- ToolExecutionFact 是持久结构化事实，协议 JSON 由其投影；
- ToolFactReader 不解析 Tool Message 字符串；
- Plugin 只能读取事实，不能修改 Conversation；
- RunScope.artifacts 指向当前 Session 私有抽屉；
- CapabilityContribution 在 Run 内冻结，Run 结束后不泄漏；
- MCP 通过新 CapabilitySource 边界后的全部旧测试保持通过；
- Skill 可只贡献 instructions，证明抽象没有被 ToolSpec 绑死。

### 16.2 `web_search` 契约测试

- 参数 Schema 与运行时校验同源；
- Provider 正常结果转换成稳定 rank、时间和 URL；
- 空结果成功；
- 厂商非法响应转换成明确外部协议错误；
- 内部 Provider 契约破坏保留原始异常；
- URL 加入当前 Run provenance；
- 有界结果进入 RunEvidence 与 ToolExecutionFact；
- 正常结果不触发整个 Tool Result 外置。

### 16.3 `web_fetch` 边界测试

- Search 历史 URL 标记为 `search_result`；
- 历史 Fetch URL 标记为 `prior_fetch`；
- 未匹配 URL 标记为 `direct`，但只要安全即可访问；
- 私网、loopback、禁止 scheme 和 Secret URL 在网络 I/O 前失败；
- 本地 Fetch 的每次重定向在下一跳网络 I/O 前复检；
- DNS 检查和实际连接目标遵循冻结的安全策略；
- timeout、DNS、TLS、429、5xx 和解析失败得到稳定 Tool Result；
- 小正文直接进入结果；
- 大正文只外置 content，保留结构化元数据和 Artifact 引用；
- Artifact 写入失败保留原始异常；
- 新 Run 通过 ToolFactReader 重建 provenance，不读取 Summary 或 Assistant 文本。

### 16.4 真实模型观察点

- 模型是否理解 Search 摘要只是候选线索；
- 需要正文时是否继续 Fetch；
- 是否会直接 Fetch 合理的公开 URL；
- 最终回答是否引用实际观察到的 URL；
- 大正文是否正确外置和分页回读；
- Provider、Artifact 或 URL Safety 失败时是否产生真实、可解释的结果。

## 17. 演化触发条件

| 能力 | 当前决定 | 只有出现以下事实才引入 |
| --- | --- | --- |
| Provider Registry | 不做 | 第二个同类 Provider 需要在同一安装中共存并切换 |
| 自动 Provider fallback | 不做 | 有数据证明单 Provider 故障是主要失败源，并能保持证据语义 |
| 网络访问授权表 | 不做 | 明确威胁模型要求限制模型外发目标，且策略同时覆盖 Search/Fetch/Browser |
| Browser Automation | 不做 | 真实任务稳定需要登录、点击、表单或 JS 交互 |
| Crawl | 不做 | 真实任务需要站点级遍历 |
| LLM 正文提炼 | 不做 | 确定性预算和 Artifact 回读在 benchmark 中不足 |
| Web 持久 Cache | 不做 | 成本或重复访问数据证明需要，并先定义失效与隐私边界 |
| 通用 ToolOutcome / Attachment | 不做 | 第二类工具也需要声明稳定 receipt 与可外置 payload |
| 通用 Plugin 基类 | 不做 | 多种 Plugin 出现稳定且重复的完整生命周期协议 |
| Run Contribution unmount | 不做 | Run 期 Capability 出现需要单独关闭的物理资源 |

任何新增能力先回答：

1. 它解决了哪个已经出现的真实任务失败？
2. 它属于稳定 Core 语义、通用 Run 期端口，还是 Web 领域实现？
3. 移除 Web Plugin 后，新增的 Core 抽象是否仍能被 MCP 或 Skill 独立说明？
4. 是否会让 Capability 入口重新承担 Provider Registry、配置、兼容和生命周期总控？

## 18. 实现前仍需确定

1. 第一版 Search Provider；
2. 第一版 Fetch Provider，以及采用本地 Fetch 还是外部 Extract 服务；
3. 本地 Fetch 时 URL Safety 的最小禁止地址集合、DNS 固定和逐跳重定向策略；
4. Web 正文内联阈值及其与 Core ToolResultLimit 的约束关系；
5. Search result 数量、title 和 snippet 的长度上限；
6. Tool Result 中 UTC timestamp 的统一字符串格式；
7. ToolExecutionFact 保存的是 Core Externalizer 后的模型可见结果，RunEvidence 如何保留当前 Run 的原始有界结果；
8. 从现有 ToolsetProvider 迁移到 CapabilitySource 的最小改动路径与兼容删除时点。

这些决策不会改变已经确认的所有权：Core 管理 RunScope、结构化事实、能力加载和共享资源治理；Web Plugin 管理 Search/Fetch 领域语义、Provider、URL Policy、来源审计和正文投影。
