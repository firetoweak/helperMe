# Phase 6C · Web 能力 MCP 接入决策

> 状态：接入策略已确定，真实任务验证待进行  
> 历史说明：原生 Web Search / Web Fetch Plugin 设计停止推进，改为记录新的能力边界与重新设计条件。

## 1. 当前决定

HelperMe 默认通过 MCP 获得 Web 能力：

- Tavily MCP 提供公开 Web 的搜索与内容提取；
- 登录、点击、填写和发布等交互，在真实需要时优先接入 Browser MCP；
- HelperMe 不实现搜索索引、网页解析器、浏览器驱动或站点自动化引擎；
- 只要 MCP 能完成真实任务，就不建立原生 Web Plugin。

这个决定不把 Web 声明为不重要，而是明确其复杂度所有权：Web 是个人助手可使用的产品能力，但不是 HelperMe Core 必须理解的领域能力。

## 2. HelperMe 保留的责任

Web MCP 与其他 MCP 使用相同的通用链路：

```text
持久 MCP 配置
→ Application 资源进入
→ Session 能力快照
→ Run 期按需加载
→ Tool Registry
→ 工具调用、结果记录与上下文预算
```

HelperMe 只负责已经存在的通用语义：

- MCP Server 的安装、启停、连接、发现与按需加载；
- ToolSpec、工具执行和失败传播；
- Tool Result、RunEvidence、Conversation 与 Artifact；
- Session 能力快照和配置变更后的明确过期；
- 已有通用审批机制能够表达的外部操作确认。

HelperMe 当前不为 Web 增加：

- 原生 `web_search` / `web_fetch` 工具契约；
- SearchProvider / FetchProvider 端口和厂商 Adapter；
- URL 来源索引或 Web 专属事实存储；
- Web 专属 SSRF、重定向和 Secret 策略；
- Provider Registry、自动探测、fallback 或持久 Cache；
- Browser Provider、浏览器会话模型或通用自动化框架。

外部 MCP 的内部实现、网络安全模型和结果语义属于该 MCP 的信任边界。当前可以接受这种黑盒程度，不为获得内部可见性而复制其实现。

## 3. 6C 验证目标

6C 不再是实现阶段，而是一次真实能力验收：

```text
安装并启用 Tavily MCP
→ Agent 按需发现和加载工具
→ 完成 search / extract / answer 任务
→ 检查结果是否足够支持真实使用
→ 记录具体缺口或确认当前方案成立
```

观察点：

1. Agent 能否发现并正确加载 Tavily 工具；
2. 搜索和内容提取是否足以完成需要公开信息的真实问题；
3. 工具结果是否受到现有上下文预算和 Artifact 机制妥善处理；
4. 外部失败能否沿现有 MCP 链路定位；
5. 最终回答是否能使用实际返回的来源信息。

验收不要求 Tavily 采用 HelperMe 自定义的 Search / Fetch 语义，也不要求建立供应商无关契约。

## 4. Browser Automation 策略

浏览器交互不是当前阶段的实现目标。真实任务需要登录、点击、填写、客户端渲染或发布信息时：

1. 先接入一个现有 Browser MCP；
2. 用目标网站和真实任务验证它是否可用；
3. 接受 MCP 对浏览器实现细节的封装；
4. 只有验证失败后，才分析失败属于 Provider 能力、MCP 协议还是 HelperMe 通用运行链路。

“HelperMe 能执行浏览器任务”不等于“HelperMe 必须拥有浏览器领域模型”。在 MCP 足够时，浏览器只是另一个可按需加载和移除的外部工具集。

## 5. 重新设计触发条件

只有出现可重复的真实失败，才重新设计 Web 或 Browser 能力，例如：

- 登录态无法在目标任务所需范围内可靠维持；
- MCP 无法表达完成任务必需的页面观察或交互；
- 工具结果无法被模型继续使用；
- 连接、取消、关闭或 Session 快照与 HelperMe 生命周期发生实际冲突；
- 现有通用审批机制无法处理已经出现的关键外部操作；
- MCP 失败无法沿通用工具链定位，且已影响真实使用。

触发后也不恢复整套旧设计。先回答：

1. 哪个真实任务失败了？
2. 缺口属于 HelperMe 还是外部 MCP？
3. 能否通过更换或配置 MCP 解决？
4. 若必须自建，最小需要拥有哪一段语义？
5. 删除这项能力时，新增复杂度能否一起删除？

只为已确认的缺口建立窄边界，不预建完整 Web 平台。

## 6. 已停止的原方案

以下内容没有获得真实需求证明，停止推进：

- 为 Web 回补通用 `RunScope`、`CapabilitySource` 和 `CapabilityContribution`；
- 建立 `WebApplicationService`、`WebRunState` 和 `UrlProvenanceIndex`；
- 分离并实现 SearchProvider / FetchProvider；
- 自行承担本地 Fetch 的 DNS 固定、逐跳重定向和 SSRF 防护；
- 为未来 Browser、Crawl、多 Provider 或 fallback 预留框架。

如果 Skill 或其他真实能力将来独立证明需要新的 Run 期公共端口，应由那个需求重新推导，而不是沿用 Web 草稿中的预设结论。

## 7. 真实验证结果（2026.08.18）

已用现有通用 MCP 链路完成以下真实任务：

- Tavily MCP：按需加载 search/extract，搜索并提取公开网页内容；长结果经 Runtime Artifact 外置后可通过 `read_artifact` 继续读取；
- Playwright MCP：完成页面导航和浏览器交互；修复连接 owner 生命周期后，跨 Run 重新加载 Toolset 不再重启 Server 或重置其领域状态；
- stdio MCP 未配置 `cwd` 时，运行目录固定到 `~/.helperme/plugins/mcp/runtime/{server_id}`，日志、截图和临时附件不再落入 HelperMe 源码目录；
- 真实 Streamable HTTP 集成、120 工具分页列表及配置 Secret 到 Artifact/日志的防泄漏扫描均已自动化覆盖。

本轮没有发现必须由 HelperMe 自建 Search、Fetch 或 Browser Provider 的缺口。6C 验收完成；后续只有出现第 5 节所列的可重复失败时才重新开启设计。

## 8. 当前结论

```text
先组合外部 MCP
→ 用真实任务验证
→ 记录可重复的具体缺口
→ 只为缺口设计窄边界
```

这满足“功能能够持续生长，而复杂度仍由一个人掌控”：HelperMe 掌握外部能力的接入与运行路径，但不要求掌握每项外部能力的内部实现。
