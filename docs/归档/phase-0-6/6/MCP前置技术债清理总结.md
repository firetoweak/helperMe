# MCP 前置技术债清理总结

## 判定原则

本次没有按历史清单直接重构，而是逐项检查是否会破坏现有契约、资源生命周期或并发正确性。确认并处理了七项真实问题：

- Toolset Provider 在每个 AgentStep 重新取规格，导致同一 Turn 的工具 Schema 和 Handler 可漂移；
- `asyncio.CancelledError` 绕过 Session 的异常落账，留下 running 状态；
- AgentApplication 可在资源启动前、关闭后或重复进入时继续使用；
- PowerShell Turn Task 取消后可能遗留子进程树和输出 reader；
- 内部创建的异步 LLM Client 没有明确关闭所有权；
- `grep`、`get_changes` 的 async handler 内部执行同步子进程，阻塞事件循环；
- JSON Schema Tool 可注册非 object 顶层 Schema，但执行器只接受 object 参数。

## 回补结果

Toolset 在 `load_toolset` 成功时只调用一次 Provider，并在 Turn 状态中保存 `ToolSpec` 元组；`ToolSpec` 本身冻结。后续 AgentStep 只复用该快照，新 Turn 才重新发现。

Session 显式处理 Task 取消：先将 Session 与 TurnRecord 落为 failed、记录 `task_cancelled` 并清理 active control，再原样传播 `CancelledError`。

AgentApplication 使用 `new → started → closing → closed` 生命周期。业务调用只允许发生在 `async with` 内；重复进入、关闭后调用均直接失败；存在活动 Turn 时拒绝关闭，进入 closing 后禁止新 Turn 与关闭过程竞争。

PowerShell 在超时、取消和内部异常路径统一终止进程树、等待进程退出并收束 stdout/stderr reader，然后保留原始异常或取消语义。

Composition 只拥有内部创建的 LLM Client，并随 Application 关闭；外部注入的 Client 仍由调用方管理。Console 显式把自己创建的 Client 作为 Application Resource 注入。

`grep` 和 `get_changes` 改用 `asyncio` 子进程。`grep` 保留流式、有界读取、超时与截断语义；`get_changes` 在仓库确认后并发读取 status 与 diff，异常传播前等待同批子进程收束。

`JsonSchemaParameters` 在构造期要求顶层 `type: object`，内部不兼容契约不再伪装成模型参数错误。

## 明确没有增加的机制

- 不增加工具语义冲突推断、静态并发安全分类或 Runtime 自动重排；
- 不把短小本地文件读写为了形式统一全部线程化；
- 不在真实 MCP Transport 和 Server 限额出现前预建通用连接调度器；
- 不吞掉内部异常或把取消转换成普通工具失败。

这些机制目前没有足够事实证明是债务，提前加入反而会让 Runtime 越权或造成过度设计。
