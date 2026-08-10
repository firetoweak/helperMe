
设计benchmark

用户目标：检查项目中的两个问题，分别修复并完成测试。

Goal: 检查并修复两个问题

Task A: 定位两个问题
Task B: 修复问题一        depends_on: A
Task C: 修复问题二        depends_on: A
Task D: 整体验证          depends_on: B, C


然后回答这 5 个设计问题：
1. Goal 与 Session 是什么关系？一个 Session 能否讨论多个 Goal？
一个 Session 可以讨论多个 Goal，但每次只能存在一个 Goal。

2. 三个 Run 如何确认属于同一个 Goal？
调用边界显式传入 goal_id，不能靠对话内容猜测。

3. Task 状态的唯一真相放在哪里？
放在 Goal 聚合中。

4. 下一个 Task 怎么产生？
建议从 pending 且所有依赖均为 done 的 Task 中计算，不额外保存 next_task。

5. TodoList 如何避免成为第二份 Task 状态？

TodoList 只描述当前 Run 怎样执行某个 Task；跨 Run 完成状态仍只写入 Goal。

