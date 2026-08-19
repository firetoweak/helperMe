## Phase 5.3 回补 B：输入/工具结果边界 总结

补齐用户输入与单次工具结果的外部边界契约；边界内直接相信契约，超过边界明确失败，不交给 Safe Compression 补救。

- 单次工具结果：Phase 5.2.1 已由 `ToolResultLimit` / Externalizer 与 ContextManager 硬检查保证有界。
- 用户输入：`SessionRuntime.start` / `resume` 在进入 Turn 前以 `MAX_USER_MESSAGE_CHARS = 32_000` 硬拒绝；超限不创建 Turn、不改 Session、不写入 Conversation。
