## Phase 5.3 回补 A.1：Runtime Mode Router 总结

每个 Turn 在追加当前用户消息后，由无状态 Router 读取最小路由投影，并严格返回 `plain/todo + reason`。投影只包含当前用户意图与上一次最终回答，不包含历史 user、tool 消息和 assistant tool calls，避免执行轨迹干扰模式分类；上一次最终回答只用于理解当前消息的指代和背景。该投影直接生成一次性 `ModelContext`，不进入 Agent 的 `ContextPreparation`、`ContextState`、压缩与 RuntimeMode 生命周期。`plain` 跳过 Todo 初始化；`todo` 进入原有 TodoMode 生命周期。Router 先判断最后一条用户消息是否明确授权执行：讨论、评价、解释或提出方向选择 `plain`；授权不明确时也选择 `plain`；只有明确要求执行后才判断是否需要 `todo`。

Router 只选择执行机制，不生成 Todo，也不承担 Planner 职责。路由响应不写入 Conversation，只记录为 `runtime_mode_routed` checkpoint。同一 Session 的不同 Turn 会重新路由。

路由选择是可降级策略，不是不可逆承诺。非法路由响应或动态 Todo 初始化协议不匹配时，Runtime 记录明确的 activation/fallback checkpoint，丢弃受限阶段响应，并在同一 Turn 使用 `PlainMode` 重新进入正常 AgentStep。局部契约保持严格，但不再把 Mode 激活失败升级成 Turn/Session 失败；显式固定 `TodoMode` 仍保持严格失败。
