# process 层测试失败记录（2026-09-18）

CLI 能力落地后跑全量 `pytest -m process` 时发现 6 个既有失败。经
`git stash` 移出全部 CLI 改动后复跑仍失败，确认与 CLI 工作无关。
这些失败在日常 `python -m pytest`（default 层）中不可见——process 标记
默认不跑。

## 失败清单

### 1. `tests/assistant/test_supervisor.py::SupervisorTest::test_resume_selected_parent_recovers_its_children_only`

现象：测试期望 `"unrelated"` 会话的日志为空，实际多出一条
`DomainFactCommitted(fact_type='session.workspace', ...)` 事件。

怀疑方向：**有真 bug 的可能，不只是测试过时**。`session.workspace`
归属事实写进了一个不该被触碰的会话，需要查多工作区提交里"会话创建时
写入归属事实"的触发路径——是 resume 流程误触发了写入，还是测试夹具
的会话创建路径本身就该更新。

### 2. `tests/assistant/test_compact.py` 5 个

- `test_background_rollover_preserves_session_tail_and_delivery_identity`
- `test_handoff_reads_frozen_prefix_attachments_from_source`
- `test_multiturn_reading_finishes_without_changing_tools`
- `test_prepared_publication_is_recovered_without_new_session`
- `test_repeated_reads_are_reminded_and_can_complete`

现象：均为 `TimeoutError`（`asyncio/timeouts.py`），真进程 compact
流程未在预期时间内完成。

怀疑方向：多工作区提交可能改变了 worker 启动/装配时序；也不排除这批
重进程测试本身对机器负载敏感。需要先确认它们在 f2d6c9f 之前是否稳定
通过。

## 时间线

- `f2d6c9f`（2026-09-18 14:55）"会话绑定工作区，多工作区注册取代全局
  单工作区"：引入 `session.workspace` 事实写入、各 Channel 按工作区
  解析沙箱边界。两批失败都指向这个提交之后。
- 2026-09-18 傍晚 CLI 能力落地时发现；stash 验证排除 CLI 因素。

## 复现

```powershell
python -m pytest -m process tests/assistant/test_supervisor.py::SupervisorTest::test_resume_selected_parent_recovers_its_children_only
python -m pytest -m process tests/assistant/test_compact.py
```

## 待办

- [ ] 确认这 6 个测试在 f2d6c9f 之前是否绿（`git stash` 回到该提交前
      复跑）
- [ ] 追查 supervisor 用例里 `session.workspace` 事实写入
      `"unrelated"` 会话的具体路径
- [ ] 追查 compact 用例超时的等待点（worker 启动？handoff 就绪？）
