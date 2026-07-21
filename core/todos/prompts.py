from __future__ import annotations

from core.todos.todo_list import TodoList


TODO_INITIALIZATION_PROMPT = """
你处于 Todo 初始化阶段，只负责为当前长任务创建可审阅的执行清单。

你不能执行任务、回答用户问题或声称已经完成任何事项。
请调用且只调用一次 rewrite_todos：
- objective 是当前执行目标摘要；
- todos 包含 2 到 6 个简短、可判断是否完成的执行意图；
- 所有 Todo 的 id 必须为 null，status 必须为 pending；
- Todo 描述要达成的局部结果，不描述具体工具调用。
""".strip()


def format_todo_instructions(todo_list: TodoList) -> str:
    lines = [
        "TodoList 是本 Run 当前的可变执行认知，不是固定命令序列。",
        f"当前目标：{todo_list.objective}",
        "当前 Todo：",
    ]
    for item in todo_list.items:
        note = f"；说明：{item.note}" if item.note else ""
        lines.append(
            f"- {item.id} [{item.status}] {item.content}{note}"
        )
    lines.extend(
        [
            "",
            "你可以根据最新事实灵活选择行动，不必机械按照 Todo 顺序执行。",
            "执行路径、任务范围或完成状态发生实质变化时，"
            "使用 rewrite_todos 提交完整最新快照。",
            "准备结束本 Run 前，必须完成最后一次 TodoList 同步。",
        ]
    )
    return "\n".join(lines)
