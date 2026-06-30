# 自主 Agent 学习计划

> 目标：逐步打基础，最终做成类似 OpenClaw 的自主 agent 助手：能持续对话、理解 workspace、使用工具完成任务、改完后可验证。
>
> 本文档是学习路线图，不是一次性任务清单。每次根据真实 run.log 复盘后调整计划，避免为了完成清单而偏离学习目标。

---

## 一、当前判断

### 北极星能力

| 能力 | 含义 | 当前项目状态 |
|------|------|--------------|
| Tool-calling loop | 模型决策 -> 执行工具 -> 结果回传 -> 继续决策 | 已有，见 `core/agent.py` |
| Workspace 文件操作 | 找文件、读、局部改、全文写 | 基本齐全 |
| 修改策略 | 何时读全、何时 patch、失败后怎么恢复 | 部分依赖 system prompt，仍需收敛 |
| 改后验证 | 知道自己实际改了什么 | 缺 `get_diff`，这是当前最大缺口 |
| 多轮会话 | 同一 session 内连续下达任务 | 未做 |
| 安全边界 | 路径限制、命令白名单、人工确认 | 后置 |
| 扩展能力 | MCP、子 agent、长期记忆等 | 远期 |

### 两次 run 暴露的问题

| 证据 | 暴露的问题 | 结论 |
|------|------------|------|
| 上周 `docs/优化.txt` | 能查文件、能 patch，但 `read_file` 截断后 old_block 不准；`OLD_BLOCK_NOT_FOUND` 后恢复不足；最后耗尽轮次 | 需要读全策略、失败恢复策略、轮次预算 |
| 今天 `run.log` | `truncated=true` 后已经会续读，多个 patch 成功；但最终总结声称“全部修改 / 修复拼写”，与磁盘事实不完全一致 | 主要瓶颈已从“做不完”变成“做完后不会验” |
| 今天 `run.log` | 一轮常只处理一个 patch，最终约 18 轮 | 效率要优化，但不是第一优先级 |
| 今天讨论 | `@register_tool` / `Field(description=...)` 属于当前项目知识 | 不应写入通用 system prompt，只能放 task prompt 或 workspace 上下文 |

### 当前核心闭环

```text
读全 -> 改 -> 验 -> 再决定 -> 诚实总结
        ^             |
        |             v
      失败恢复 <- 真实反馈
```

现在最缺的是“验”。没有 `get_diff`，agent 只能根据自己的工具调用记忆总结，容易把“计划改了”说成“已经改了”。

---

## 二、计划原则

1. **先建立可观测闭环，再优化聪明程度。**  
   `get_diff` 比继续堆 prompt 更急，因为它让 agent 看见真实结果。

2. **system prompt 只写通用行为策略。**  
   例如读全、失败恢复、改后验证、不要夸大总结。当前项目里的 `@register_tool`、`Field(description=...)` 不属于通用 agent 规则。

3. **每次只改一层。**  
   不要同时改 prompt、工具返回值和测试任务，否则 run.log 里看不出是谁起作用。

4. **用 run.log 和 diff 说话。**  
   成功不是“最后有自然语言回答”，而是回答内容能被工具结果和 diff 验证。

5. **学习目标优先于代码完成速度。**  
   你要能解释为什么 agent 这么做，而不是只让 AI 把代码改完。

---

## 三、近期路线

### 阶段 0：复盘校准（已完成一半）

**目的**：把“感觉 agent 不稳”变成可解释的证据。

**已得到的判断**

- 上周失败的主因不是工具不可用，而是截断、patch 精确匹配、失败恢复和轮次预算。
- 今天 run 证明 `truncated=true` 规则有效，agent 会继续读取。
- 今天 run 同时证明缺少 `get_diff` 会导致总结不可信。

**还要补的动作**

| 任务 | 做法 | 验收 |
|------|------|------|
| 写一条复盘结论 | 在学习笔记或本文件中口头总结两次 run 的差异 | 能说清“上周是做不完，今天是不会验” |
| 确认当前测试题角色 | 把“优化工具描述”视为 benchmark，不视为通用 agent 规则来源 | 不再把 `Field(description=...)` 写进 system prompt |

---

### 阶段 1：补 `get_diff`，建立改后验证闭环（当前第一优先级）

**目的**：让 agent 在总结前能看到真实磁盘改动，避免过度自信。

#### 1.1 实现 `get_diff`

| 项 | 内容 |
|----|------|
| 新建 | `tools/file_diff.py`，或放入合适的现有 tools 模块 |
| 行为 | 优先返回 `git diff`；如果不是 git 仓库，返回明确错误 |
| 范围 | 默认查看 workspace 当前未提交改动；第一版不用支持复杂参数 |
| 注册 | 在 `tools/__init__.py` 导入，让 tool registry 能发现 |
| 学什么 | agent 需要闭环反馈，不能只相信 `PATCH_APPLIED` |

**建议第一版输入**

```text
path: 可选，限制查看某个文件或目录的 diff
```

第一版可以先不做 staged / unstaged 区分，先把闭环跑通。

#### 1.2 system prompt 增加验证规则

只加通用规则，不写当前项目细节：

```text
完成文件修改后，必须调用 get_diff 查看实际改动。
最终总结只能基于 get_diff 中真实出现的改动。
如果计划修改了某处但 diff 中没有出现，必须说明“未完成”，不能声称已经修改。
```

#### 1.3 验收测试

用同一个 benchmark：

```text
[用户提问] 你觉得项目的工具描述是不是有点像一个code agent？
你帮我优化一下描述，让它更像一个通用智能体。
```

| 验收项 | 标准 |
|--------|------|
| 能完成 | 不再出现“工具调用次数过多” |
| 能验证 | trace 中出现 `get_diff` |
| 总结诚实 | 回答中的修改文件、修改范围与 diff 一致 |
| 不过度声称 | 如果 `Field(description=...)` 没改，不说“拼写都修了” |

---

### 阶段 2：收敛最小通用 system prompt

**目的**：把 system prompt 从“临时补丁”整理成通用 agent 行为规则。

#### 2.1 必须保留的通用规则

```text
文件修改策略：
1. 修改文件前先读取目标片段；old_block 必须逐字来自 read_file / grep 返回的原文。
2. 如果 read_file 返回 truncated=true，必须用 next_offset 续读，拿到完整目标片段后再 patch。
3. 收到 OLD_BLOCK_NOT_FOUND 后，重新读取目标区域，用最新原文重试一次。
4. 同一位置连续两次 patch 失败，停止工具调用，向用户说明原因和已完成部分。
5. 完成修改后调用 get_diff；最终总结必须与 diff 一致。
```

#### 2.2 暂时不要写入 system prompt 的内容

这些是任务知识或项目知识，不是通用 agent 底层规则：

| 内容 | 应放位置 |
|------|----------|
| `@register_tool` 文档和 `Field(description=...)` 是不同位置 | benchmark prompt / workspace hint |
| “优化工具描述”要改哪些具体文件 | 当前任务 prompt |
| 本项目工具文件分布在 `tools/file_read.py`、`tools/file_write.py` 等 | workspace 上下文或 agent 自己探索得到 |
| 这次测试希望轮次小于 10 | benchmark 验收标准 |

#### 2.3 验收

- 你能解释 system prompt、task prompt、workspace context 的区别。
- system prompt 中没有当前项目专属知识。
- 同一 benchmark 至少跑 2 次，trace 中能看到读全、patch、diff、总结的闭环。

---

### 阶段 3：工具反馈优化（在 `get_diff` 后做）

**目的**：如果 prompt + diff 仍不稳定，再让工具返回值给模型更明确的 teaching signal。

#### 3.1 `read_file` 截断反馈

| 项 | 内容 |
|----|------|
| 文件 | `tools/file_read.py` |
| 改动 | `truncated=true` 时增加 `hint` |
| hint 示例 | `内容已截断，继续修改前请用 next_offset 续读目标片段` |
| 是否当前必做 | 否。今天 run 已证明 prompt 能让模型续读，先观察 |

#### 3.2 `apply_patch` 失败反馈

| 项 | 内容 |
|----|------|
| 文件 | `tools/file_write.py` |
| 改动 | `OLD_BLOCK_NOT_FOUND` 时增加 `hint` |
| hint 示例 | `请重新 read_file 获取最新原文；old_block 必须逐字复制，不要凭记忆构造` |
| 是否当前必做 | 否。等 `get_diff` 跑通后再判断 |

#### 3.3 验收

故意构造一次 patch 失败，看下一轮是否走：

```text
失败 -> 重新读取 -> 用最新原文重试 -> 仍失败则停止总结
```

---

### 阶段 4：效率与任务分层

**目的**：在结果可信后，再降低轮次和耗时。

#### 4.1 并行读，谨慎并行改

| 场景 | 策略 |
|------|------|
| 多个文件互不依赖的读取 | 可以同一轮并行 read_file |
| 多个文件互不依赖的 patch | 可以考虑同一轮多个 tool call，但先保证 old_block 都来自已读原文 |
| 同一文件多个 patch | 初期不追求并行，避免上下文失效；可读 -> patch -> diff 后再继续 |

#### 4.2 benchmark 任务拆分

“优化工具描述”是测试题，不是通用规则。为了观察 agent 能力，可以拆成：

| 子任务 | 目的 |
|--------|------|
| 只改 `tools/demo.py` | 验证最小读改验闭环 |
| 只改 `tools/file_read.py` | 验证截断续读和多个工具描述 |
| 改 `tools/file_write.py` + `tools/file_manage.py` | 验证多文件修改 |
| 全量优化工具描述 | 最终综合测试 |

#### 4.3 验收

- 在总结可信的前提下，再看轮次是否能从约 18 降到 10 左右。
- 如果轮次下降但总结不可信，不算进步。

---

### 阶段 5：多轮对话

**目的**：从“一问一答脚本”变成持续助手。

| 任务 | 内容 |
|------|------|
| REPL 会话 | `while True: input -> agent.run(...)`，不每轮重建 `Conversation` |
| 轮次策略 | 明确 `max_rounds` 是每个 user 消息独立计算，还是整个 session 共享 |
| 分阶段任务测试 | 第一轮只改一个文件，第二轮继续，让 agent 利用上下文 |

**验收**

- 终端可连续输入。
- 第二轮不需要重新从零探索整个项目。
- 文档中解释清楚会话状态如何保留。

---

### 阶段 6：安全与工程化

这部分后置，等读改验闭环稳定后再做。

| 任务 | 文件/模块 | 说明 |
|------|-----------|------|
| workspace 边界强化 | `tools/workspace.py` | 路径逃逸、敏感目录、错误信息 |
| 空响应处理 | `core/llm_client.py`、`core/agent.py` | 单独状态、重试或明确失败 |
| `run_command` | 新工具 | 只允许白名单命令，必要时人工确认 |
| grep 增强 | `tools/file_read.py` | `-i`、`-F`、glob 过滤等 |

---

### 阶段 7：OpenClaw 类体验延伸

完成阶段 1～5 后再细化。

```text
稳定单 agent loop
  -> 多轮会话
  -> 摘要 / 记忆
  -> MCP / 外部工具
  -> 子任务分解 / 子 agent
  -> CLI 或 UI 产品化
```

每加一层前先问：上一层 loop 是否已经稳定可测？

---

## Rule 同步区（给 Cursor 看，阶段推进时只改本节）

| 项 | 当前值 |
|----|--------|
| **当前阶段** | 4：单文件与完整任务都已跑通 `read_file → patch → get_changes → 基于 diff 总结`，进入 Plan Gate 与最小 system prompt 收敛 |
| **本轮优先** | 收敛通用 system prompt，重点压住两件事：无目标 workspace 扫描、最终总结夸大 diff |
| **本轮禁止** | 未经用户授权的多层同时改（executor + prompt + 批量 docstring）；效率优化、多轮 REPL；把 benchmark 细节写进 system prompt |
| **下一验收** | 重跑完整任务：trace 先限定目标文件范围，再 `read_file → apply_patch/replace_all → get_changes`；最终总结不使用“所有/全部/完全统一”等超出 diff 的表述 |
| **用户触发词** | 「好/直接改」= 可写代码；「建议/为什么」= 只教不改 |

> AI 动手前须读本表；与用户请求冲突时，先指出冲突并让用户选择，不擅自跳阶段。

---

## 四、当前推荐顺序

| 顺序 | 任务 | 状态 | 主要文件 | 学到什么 |
|:----:|------|:----:|----------|----------|
| 0 | 复盘两次 run | 进行中 | `docs/优化.txt`、`run.log` | 用证据判断瓶颈 |
| 1 | 实现 `get_changes` | 已完成 | `tools/get_changes.py` | 改后验证闭环 |
| 2 | prompt 增加验证规则 | 已完成 | `core/agent.py` | 总结必须基于事实 |
| 3 | 重跑 benchmark | 单文件与完整任务均已首次跑通 | `run_2026-06-30.log` | 验证“诚实总结” |
| 4 | 收敛最小 system prompt | **当前重点** | `core/agent.py` | Plan Gate、通用策略 vs 任务知识 |
| 5 | 工具 hint | 观察后再定 | `tools/file_read.py`、`tools/file_write.py` | 工具返回值作为 teaching signal |
| 6 | 效率优化 | 后置 | `core/agent.py` | 少轮次但不牺牲可信度 |
| 7 | 多轮对话 | 后置 | `core/agent.py` / `main.py` | 会话状态 |

---

## 五、下一次动手建议

下一步只做一件事：收敛最小通用 system prompt。

背景判断：

- `get_changes` 已经承担原计划中 `get_diff` 的职责。
- 2026-06-30 的单文件 benchmark 已出现 `read_file -> apply_patch -> get_changes -> 基于 diff 总结`。
- 2026-06-30 的完整任务 benchmark 也能多文件修改并调用 `get_changes`，但开头仍有 `config.yaml`、`**/*` 等偏宽泛探索。
- `read_file` 的分页元信息、`glob` 的路径语义、核心 docstring 已做过一轮收敛。
- 最终总结仍有轻微夸大风险，例如说“所有工具都采用统一结构”，但 diff 里并非所有工具都完全统一。
- 空响应仍存在，但目前更像模型噪声；只要能恢复到闭环，暂不作为主线问题。

建议先自己回答这三个问题，再改 prompt：

1. 哪些规则是通用 agent 行为策略，适合放 system prompt？
2. 哪些规则只是本项目 benchmark 知识，不能写进 system prompt？
3. Plan Gate 应该阻止“探索本身”，还是阻止“没有目标和退出条件的探索”？
4. 最终总结里哪些词会天然导致夸大，比如“全部”“所有”“完全统一”？

推荐第一版保留的通用策略：

```text
1. 文件修改前先形成最小文件操作计划。
2. 目标文件未知时，先根据用户意图推断候选目录；优先用窄范围 glob/grep 定位，避免先扫整个 workspace。
3. read_file 返回 truncated=true 时，用 next_offset 续读目标片段。
4. old_block 必须来自 read_file/grep 返回的真实原文。
5. patch 失败后根据 code/hint 恢复，不能假装成功。
6. 修改后必须调用 get_changes，最终总结只基于 diff/status。
7. 最终总结避免使用超出 diff 证据的绝对化表述；如果只是部分工具/部分文件改了，就说“部分”。
```

---

## 六、进度勾选

### 复盘与认知

- [x] 确认上周失败不是工具完全不可用
- [x] 确认今天主要瓶颈是缺少验证
- [x] 确认 `@register_tool` / `Field(description=...)` 不属于通用 system prompt
- [ ] 能口头解释 system prompt、task prompt、workspace context 的区别

### 当前闭环

- [x] `max_rounds` 默认值已从 10 调到 20
- [x] system prompt 已有 `truncated=true` 续读规则
- [x] 实现 `get_changes`（原计划的 get_diff）
- [x] system prompt 增加“修改后必须 get_changes”
- [x] benchmark 首次跑通 `patch -> get_changes -> 诚实总结`
- [x] 完整任务首次跑通多文件修改并调用 `get_changes`
- [x] `read_file` 返回元信息修正：不截半行，`end_line/next_offset/truncated_by` 与实际内容一致
- [x] `glob` 路径语义修正：支持 `glob(path="tools", pattern="file_read.py")`

### 后续稳定性

- [ ] 收敛最小通用 system prompt
- [ ] Plan Gate 收敛：完整任务开头不再先读配置文件或宽泛扫描 `**/*`
- [ ] 总结口径收敛：最终回答只说 diff 证明的范围，避免“所有工具/全部完成”等夸大表述
- [ ] benchmark 连续跑 2 次，确认闭环稳定而不是偶然成功
- [ ] 评估是否需要 `read_file` hint
- [ ] 评估是否需要 `apply_patch` hint
- [ ] 在可信基础上优化轮次
- [ ] 记录空响应为模型噪声，后续只做轻量统计或兜底，不作为当前主线

### 远期

- [ ] 多轮 REPL
- [ ] workspace 安全边界强化
- [ ] 空响应处理
- [ ] 可选 `run_command`

---

## 七、相关文件索引

| 文件 | 作用 |
|------|------|
| `core/agent.py` | loop、max_rounds、system prompt、run.log |
| `core/messages.py` | Conversation 消息管理 |
| `core/llm_client.py` | LLM 调用与响应解析 |
| `core/tools_executor.py` | 工具执行入口 |
| `tools/file_read.py` | read_file、glob、grep |
| `tools/file_write.py` | apply_patch、replace_all |
| `tools/file_manage.py` | write_file |
| `tools/workspace.py` | 路径解析与安全 |
| `README.md` | 项目分期总览 |
| `docs/优化.txt` | 上周失败案例分析 |
| `run.log` | 今天运行 trace |
| `tests/run_agent_parallel.py` | 批量运行 agent；改文件任务请串行 |

---

## 八、学习原则

1. **先自己判断，再让 AI 对照。** 不要直接让 AI 给最终答案，要先说出你的怀疑点。
2. **每次只验证一个变量。** 改了 prompt，就先别同时改工具 hint。
3. **成功必须可观察。** 没有 diff 或日志证据，不算真正稳定。
4. **警惕 benchmark 过拟合。** 当前测试题可以帮助发现问题，但不能直接变成通用 agent 规则。
5. **保留质疑。** 你刚才质疑 1.5 是对的，这类质疑正是学习 agent 架构最重要的能力。

---

*文档版本：2026-06-30 晚 · 根据 `run_2026-06-29.log` 与 `run_2026-06-30.log` 对照更新*
