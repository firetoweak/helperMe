## Phase 0 · Agent Core 总结

目标是做一个能够调用工具，并且能够读写文件的agent最小MCP。

### 学习内容

1. 最小agent loop
2. 消息拼接格式
3. 调用工具的openAI api怎么定义的
4. 工具是如何注册和描述
5. 读写文件工具要做哪些？

- 每个暴露给模型的工具描述必须回答四件事：工具做什么；什么时候使用以及代替什么；危险行为或关键限制；结果截断或失败后如何继续。

### Benchmark

提问：你觉得项目的工具描述是不是有点像一个code agent？你帮我优化一下描述，让它更像一个通用智能体。
这个测试提问，agent能完成执行，并不出错（已达成 2026.06.30）
修改正确✓ 不死循环✓ get_changes一致✓ 最终总结真实✓

### 模块状态

✓ OpenAI Protocol
简单的openAI api
有：标准调用格式，支持配置api 和工具调用
缺：无流式

✓ Message

有：基础状态，消息拼装
缺：无trace跟踪

✓ Tool Registry

有：工具注册表

✓ Tool Execute

有：ok code data...输出标准格式

✓ Loop

有：完整一轮对话工具调用循环，system_prompt
缺：没有runtime

✓ Workspace

有：基础工作空间；Phase 5.5 已补充由 Composition Root 配置并注入的多根轻量路径沙箱。每个 root 使用独立 WorkspaceSandbox，文件工具只接受 root 名称与 root 内相对路径。
边界：只约束经过 Workspace 工具的路径访问，不做进程/容器隔离。

✓ Verification

有：git diff 只能看到相比上次提交前是否改动此文件
缺：以后优化成真正的改动地方检测
