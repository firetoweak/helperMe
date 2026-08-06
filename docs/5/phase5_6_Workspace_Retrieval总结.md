## Phase 5.6 Workspace Retrieval 总结（完成于 2026.08.05）

目标：在 Workspace Sandbox 边界内提供有界、真实且可继续的只读回取工具；Workspace 仍是外部事实源，不自动注入 ModelContext。

核心结论：Workspace Retrieval 是模型显式调用的工具链，不是统一检索层。`glob` 按名称找路径，`grep` 按内容找匹配行，`read_file` 按行读取正文；三者通过窄结果逐步收敛信息需求。

### 职责边界

```text
get_workspace_info  发现逻辑 root
        ↓
glob                按名称定位路径
grep                按内容定位匹配行
read_file           读取确定位置的正文窗口
        ↓
Tool Result         作为事实写入 Conversation
```

- 所有路径继续使用 `root + 相对 path`，并经过 5.5 的 Workspace Sandbox。
- 不扫描并自动注入 Workspace 内容，不修改 ContextState，不引入向量索引、语义检索或 Unified Retrieval。
- `get_workspace_info` 只暴露逻辑 root 名称；物理路径与系统平台是内部配置，不提供给模型。

### 统一分页契约

- `glob` 与 `grep` 使用从 0 开始的结果 `offset`；`read_file` 使用从 1 开始的行 `offset`。
- 单次结果通过 `max_results`、`limit` 和字符预算限制。
- 工具只向前多读取一条结果判断是否还有后续，不为精确总数遍历完整数据源。
- `truncated=true` 时必须提供可执行的 `next_offset`；不再返回 `total`、`total_hits` 或 `total_lines`。

### read_file

- 最大读取文件为 20 MiB；超过后返回 `FILE_TOO_LARGE`，应先用 `grep` 定位或改用专用工具。
- 采用流式逐行读取，单次最多 2000 行和 8000 字符。
- 普通字符截断保留完整行，`next_offset` 指向尚未读取的下一行。
- 单行自身超过 8000 字符时返回 `LINE_TOO_LONG` 与有界 preview，不把不可继续的部分伪装成分页成功结果。
- 空文件从第一行读取成功；非空文件越过末尾返回 `OFFSET_OUT_OF_RANGE`。

### grep

- 一条 hit 表示一条匹配行；同一行内的多个匹配位置保存在 `submatches`。
- 单条匹配正文、submatches 数量和整页正文字数都有独立上限；截断字段明确提示模型改用 `read_file`。
- 不返回上下文；模型使用 hit 的文件和行号调用 `read_file` 获取完整上下文。
- `rg --json` 按路径排序并流式消费；获得额外一条匹配后立即停止，以保证截断判断真实且结果顺序可分页。
- `rg` 具有执行超时，所有成功、失败和异常路径都会回收子进程与管道。

### glob

- 使用稳定的名称排序和深度优先遍历，支持结果 offset 分页。
- 不含 `/` 的 `pattern` 递归匹配名称；含 `/` 时从搜索起点匹配逻辑路径；保留 `kind` 与 `max_depth` 收窄能力。
- 不递归进入符号链接目录；指向 Workspace 外的符号链接结果被过滤。

### 验收

- Workspace 信息、读取边界、空文件、超大文件、超长单行、grep 有界分页、超时清理、glob 稳定分页与显式进入 ModelContext 均有独立行为测试。
- 完整测试通过；Windows 当前环境无符号链接创建权限时，对应真实符号链接用例跳过。

### 第一版明确不做

- Workspace 自动注入 Context。
- 语义检索、向量索引、统一检索层或长期记忆检索。
- 超大文件读取、超长单行的字符级续读或文件行号索引；超长行改为明确失败并返回有界 preview。
- 为实时变化的 Workspace 提供跨调用快照一致性；分页期间文件变化时应重新搜索。
