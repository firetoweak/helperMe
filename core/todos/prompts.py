from __future__ import annotations

from core.todos.todo_list import TodoList


TODO_INITIALIZATION_PROMPT = """
你处于 Todo 初始化阶段，只负责为当前复杂任务创建可审阅的执行清单。

你不能执行任务、回答用户问题或声称任何事项已经完成。
请调用且只调用一次 rewrite_todos。

要求：
- objective 用一句话描述本 Run 最终要达成的结果；
- todos 包含 2 到 6 个简短、可验收的执行意图；
- 所有 Todo 的 id 必须为 null，status 必须为 pending；
- Todo 描述应达成的局部结果，不描述具体工具调用；
- 每个 Todo 完成后，应能明确说明：
  - 得到了什么结论，或
  - 产生了什么可交付结果，或
  - 通过了什么验证；
- 清单应覆盖完成目标所需的关键判断、核心产出和必要验证；
- 只在确实影响后续决策时，才设置调查类 Todo；
- 不要使用“了解、查看、研究、分析、思考、评估”等空泛过程作为独立 Todo；
- 不要预设未经证实的结论或方案；
- 不要为了凑数量拆分显而易见的连续动作；
- 清单是当前最佳执行假设，后续可根据新事实整体重写。
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
            "Todo 描述应达成的结果，具体行动由你自行选择。"
            "可以偏离原顺序，但行动必须服务于当前目标或促成 Todo 重构。"
            "获取信息本身不算完成，必须形成结论、产出或验证结果；"
            "若连续行动没有带来新的判断、产出或决策变化，应停止扩展过程，"
            "转向收敛、交付、验证或重写 TodoList。"
            "某一 Todo 已具备可写进 note 的验收依据后，应立即同步并推进，"
            "不要为同一验收目标继续扩大探索。",
            "",
            "当新事实改变原有判断，目标、范围、约束或优先级发生变化，"
            "原 Todo 过于宽泛、重复或无法验收，出现新的必要产出或验证项，"
            "需要拆分、合并、取消或调整顺序，或当前清单已不能准确表达"
            "下一步执行认知时，调用 rewrite_todos。"
            "不要只机械更新 status；执行认知变化时应同步修改 objective、"
            "Todo 内容、结构或说明。",
            "",
            "只有形成明确结论、完成可交付结果或得到验证后，才可将 Todo "
            "标记为 done；done 的 note 应简要记录结果或依据，cancelled 的 "
            "note 应说明取消原因。准备结束本 Run 前，必须完成最后一次 "
            "TodoList 同步。最终回答必须面向用户说明：达成了什么结果、"
            "关键依据是什么、还有哪些未决事项；"
            "禁止仅用「Todo 已同步/任务已完成」作为最终回答。",
        ]
    )

    return "\n".join(lines)