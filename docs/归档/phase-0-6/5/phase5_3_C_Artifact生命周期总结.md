## Phase 5.3 回补 C：Artifact 生命周期 总结

Runtime Artifact 是 Conversation 的外部正文，不是应用退出时可丢弃的临时缓存。Session 被定义为独立的“工作抽屉”，持有 Conversation、ContextState、运行记录与私有 Artifact；Artifact 引用只在所属 Session 内有效。

- Composition Root 为每个 Session 组装绑定私有 ArtifactStore 的 TurnRuntime；工具结果外置、Level 1 脱水与 `read_artifact` 使用同一个 Session 抽屉，不把 `session_id` 泄漏到 TurnRuntime 以下。
- 文件系统第一版按 Session 隔离 Artifact 目录；重建同一抽屉的 FileArtifactStore 后，已有 `artifact_id` 仍可回读，为以后 Session/Conversation 持久化保留稳定引用。
- Turn 完成、Session completed/blocked/failed、Level 2 裁剪 `tool_artifacts` 或应用退出都不自动删除 Artifact。
- 只有显式 `delete_session` 才整体删除 Session 抽屉；正在执行的 Session 拒绝删除，资源删除失败保留 Session 状态并直接暴露原始异常。
- 不做 TTL、后台清理、逐 Artifact 引用计数或跨 Session 共享。未来切换数据库时由 Session 持久化层负责保存引用关系与永久删除事务，不改变 `artifact_id` / `read_artifact` 契约。

本次只补 Artifact 生命周期与显式删除用例；Conversation、ContextState、Event 和 Turn Trace 的落盘恢复仍属于后续 Session Persistence，不在本回补中提前实现。
