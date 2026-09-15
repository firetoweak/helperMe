# LiteLLM 接入

> 状态：已实现。

## 目标

HelperMe 使用进程内 LiteLLM Router 作为模型适配层。LiteLLM 负责 Provider 配置、协议转换和未来可选的路由能力；Assistant 继续只依赖窄 `LLMApi`，Runtime、Journal 与 `ModelDecision` 不认识 LiteLLM 或厂商字段。

```text
Assistant → LLMApi → LiteLLMAdapter → litellm.Router → Provider
```

本次接入解决已经发生的协议差异，不引入 LiteLLM Proxy，不接管 HelperMe 的 Agent 循环、工具执行、上下文投影或持久化。

## 配置所有权

HelperMe 只校验模型配置的外层形状：当前逻辑模型名与 Router 配置必须存在。Router 配置内部不枚举模型、Provider 或参数字段，原样交给 LiteLLM 校验和解释。

```json
{
  "model": {
    "active": "assistant",
    "router": {
      "model_list": [
        {
          "model_name": "assistant",
          "litellm_params": {
             "model": "deepseek/deepseek-v4-pro",
             "api_key": "...",
             "reasoning_effort": "high"
          }
        }
      ],
      "num_retries": 0
    }
  }
}
```

这是传给 Python SDK `Router` 的数据形状，不复用 LiteLLM Proxy 的配置加载器，也不承诺 Proxy 专属的 `include`、密钥引用或热更新语义。

`runtime.model_context_limit`、输入预算和 Compact 阈值仍由 HelperMe 配置。第一阶段一个逻辑模型只对应一个部署，关闭重试，不配置 fallback、缓存或跨模型负载均衡。

## LLMApi 契约

`LiteLLMAdapter` 把 LiteLLM 响应收成两部分：

1. `content`、`tool_calls`、`usage` 转换为 HelperMe 已有的归一化结果；
2. assistant message 中其余非空字段转换为普通 JSON `message_extensions`。

`message_extensions` 是模型协议附件，不是对话事实，也不是 `ModelDecision` 的组成部分。它不得包含由 HelperMe 拥有的 `role`、`content`、`tool_calls`。空字符串是实际值，必须保留；`null` 与不存在的可选字段不写入。

LiteLLM 对象、Pydantic 类型和异常类型不得越过 `helperme/llm`。Adapter 之外的生产代码不得 import LiteLLM。

## 流式调用

`LiteLLMAdapter` 默认使用 LiteLLM 流式调用，不保留另一条非流式主链路。`LLMApi.chat()` 可以接收可选的 `on_content_delta`；它只观察标准化后的正文增量，Compact、Skill 摘要等不需要展示的调用不传入观察者。

所有 chunk 仍由 LiteLLM 的 `stream_chunk_builder` 组装为一次完整响应，再进入现有 `LLMResponse`、usage 与 `message_extensions` 校验。HelperMe 不自行拼接工具调用、推理字段或 Provider 扩展。已经交给观察者的正文增量拼接后必须与最终 `LLMResponse.content` 完全一致，否则整次响应无效。

正文增量只是调用期间的可丢弃观察结果，不是 Event、ModelDecision 或第二事实源。Assistant 用当前 trigger event id 作为稳定 `output_id`，经 Worker → Host 的单向 preview 信号送到 Channel；Runtime API、Event 和状态投影均不感知流式展示。

## 持久化与重放

一次模型响应提交为 Step 时，将非空 `message_extensions` 写入已有 `decision_metadata`，与 LoopGuard 的 metadata key 显式组合。Runtime 只把它当作不透明 JSON 原子持久化。

投影 `StepCommitted` 时：

```text
decision_metadata.message_extensions
        + ModelDecision 重建的 content
        + Commands 重建的 tool_calls / command_id
        → assistant 协议消息
```

扩展字段不能覆盖核心字段。不能把原始 assistant message 整包当成下一轮历史，因为原始 tool call id 不是 Runtime 提交后的 command id，控制命令和 deliver 也可能改变最终可见投影。

扩展字段在模型协议投影时恢复，发生在预算评估之前。因此它们参与实际输入预算、Compact 和窗口继承；不可在 `chat()` 前临时补入，否则预算会失真，Compact 后也会丢失。

只有当前 Context Window 可见且确实投影出 assistant message 的 Step 才携带扩展字段。窗口切换继续沿用现有可见性和跳过规则，不另建推理历史规则。

## 异常与生命周期

Adapter 只转换 LiteLLM 明确定义且 HelperMe 已有语义对应的异常：认证与权限、上下文超限、连接/超时/限流/服务端暂态错误及其他已知 Provider 错误。无确定对应关系的异常原样暴露，不以“模型不可用”或空响应继续运行。

Router 随 Worker 生命周期创建和关闭。配置错误在装配边界暴露，不补默认 Provider、不猜测模型名、不降级到其他模型。

## 版本与路由演进

LiteLLM 使用精确版本，不使用开放上界自动升级。每次升级单独验证，不把新版功能自动接入 HelperMe。

未来启用 LiteLLM 已有的权重、顺序 fallback 或负载均衡时，原则上只修改透传给 Router 的配置。但启用前必须先解决两件事：

- 决策证据能够记录一次调用实际命中的公开模型/部署身份；
- 同一逻辑模型下不同部署的上下文窗口不会使请求前预算失去确定依据。

需要 HelperMe 根据任务语义选择模型、传递 Session 路由标签或改变上下文策略时，属于新的产品设计，不伪装成纯配置更新。

## 验证

实现至少覆盖：

- 配置外层精确校验，Router 内部字段完整透传；
- 普通文本响应、单个与并行工具调用、usage 和已知异常映射；
- `message_extensions` 保留未知 JSON 字段和空字符串，拒绝非 JSON 值及核心字段冲突；
- metadata 与 LoopGuard 同时存在时互不覆盖；
- 投影只把扩展字段贴到对应的可见 assistant 消息，核心字段仍来自已提交事实；
- DeepSeek thinking 的两拍工具调用；
- Worker 重启后的 Journal 重放继续调用；
- Compact 切换窗口后继续调用；
- 使用上一固定 LiteLLM 版本产生的持久消息，通过候选新版本完成续拍。

前五项为机械边界测试；后四项使用真实模型或版本升级夹具验证。LiteLLM 的 Provider 数量和内部实现不在 HelperMe 单元测试中重复穷举。
