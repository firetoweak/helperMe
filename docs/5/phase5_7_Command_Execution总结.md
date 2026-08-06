## Phase 5.7 Command Execution 总结

状态：第一版实现与行为测试完成；真实 Agent Benchmark 待验收。

### 完成内容

- 新增 `execute_command` 工具，接受逻辑 Workspace root、相对 cwd、完整 PowerShell command 和有限 timeout。
- 新增 PowerShell Runner，显式启动 `powershell.exe`，不使用 `shell=True`。
- stdin 固定为不可交互，第一版不支持 PTY、后台任务和自动重试。
- stdout 与 stderr 使用独立线程并发排空，避免单侧管道写满导致死锁。
- 输出在读取阶段进行头尾有界保留，不依赖 Context Compression 或事后 Artifact 外置补救。
- 非零退出码作为 `COMMAND_COMPLETED` 的真实结果返回；超时返回 `COMMAND_TIMEOUT` 和已捕获的有限输出。
- 子进程环境由白名单策略构造，禁止直接继承完整 `os.environ`。
- cwd 继续复用 WorkspaceSandbox；只约束启动目录，不检查命令中的绝对路径，也不宣称进程安全隔离。
- Composition Root 负责绑定 Workspace 和共享 Runner，RunRuntime 与 ToolsExecutor 不感知 PowerShell 实现。
- 命令以 `workspace_effect=read_only|may_write` 声明预期副作用，默认保守使用 `may_write`；StopGuard 只要求已执行或超时的 `may_write` 命令在停止前调用 `get_changes`，只读查询不会被验证流程拉偏。

### 关键结论

Command Execution 是外部能力工具，不是新的 Agent Runtime。工具适配层负责 Workspace 与协议边界，Runner 负责进程生命周期，Capture 负责输出边界；三者不进入 Agent Loop 内部。

WorkspaceSandbox 不是命令沙箱。命令可以使用绝对路径、启动子程序和访问网络；第一版只适用于用户明确指定并信任的本机项目。

Runtime Artifact 解决模型上下文中的大结果，不自动解决子进程输出的内存风险。因此输出限制必须发生在流读取阶段。

### 已验证行为

- PowerShell 参数、管道和组合语义。
- stdout、stderr 分离与显式退出码。
- UTF-8 输出。
- Workspace 相对 cwd 与越界 cwd 拒绝。
- 命令使用 Workspace 外绝对路径。
- 环境变量白名单及未授权变量隔离。
- 大输出头尾截断与原始规模统计。
- 超时、部分输出返回和 Windows 进程树终止。
- 非零退出不破坏后续工具调用。
- 命令执行后的 StopGuard 验证要求。

### 后续验收

使用计划中的临时 Git 项目运行真实 Agent Benchmark，验证 Agent 能自主完成依赖安装、失败测试定位、代码修改、重新测试、构建和最终 Git diff。Benchmark 通过后，再将 Phase 5.7 标记为完成。
