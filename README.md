# helperMe

`helperMe` 是我为自己构建的个人通用助手。

我使用过其他 Agent 助手，但当我想把它们改造成自己希望的样子时，经常一处修改牵动全身；修改没有生效，也很难判断问题发生在哪里。功能属于它们，我却没有真正掌握它们。

因此，我开始构建 helperMe。

我希望它不仅能完成任务，也始终能被个人理解、修改、验证并持续塑造。学习是构建它的方式，成为一个长期可用的个人助手才是最终结果。

## 我相信什么

真正属于个人的助手，应当把三种权力交还给个人：

- 理解它为什么这样运行。
- 控制能力、状态和行动如何生效。
- 按自己的需要持续改变它，并验证改变的结果。

helperMe 不以成为通用 Agent Framework 为目标，也不为未知用户预建任意组合能力。它只为已经发生或明确规划的个人需求建立扩展边界。

我仍然重视高内聚、低耦合和优雅抽象，但这些不是形式上的高级感：

- 高内聚意味着同一种变化集中在一处。
- 低耦合意味着一次需求变化只影响局部。
- 可扩展意味着助手能沿着真实需要持续生长。
- 好抽象应让系统更容易理解和验证，而不是增加无法追踪的因果链。

> 功能可以持续生长，但复杂度仍应由一个人掌控。

更完整的设计立场见[个人助手的可控扩展](docs/专题/个人助手的可控扩展.md)，学习过程与当前路线见[自主 Agent 学习计划](docs/自主Agent学习计划.md)。

## 如何使用

项目使用 Python 和 PowerShell，并调用 OpenAI 兼容的 Chat Completions 接口。

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item model_config.example.yaml model_config.yaml
```

编辑 `model_config.yaml`，填写模型与工作区配置：

```yaml
model:
  name: "your-model-name"
  base_url: "https://your-model-endpoint.example/v1"
  api_key: "your-api-key"

workspace:
  root: "D:\\work\\agent"
```

启动助手：

```powershell
python console_chat.py
```

进入对话后：

- 直接输入内容，执行一次普通 Agent Run。
- 输入 `/goal <目标>`，启动可持续执行和独立验证的 Goal 工作流。
- 用自然语言要求安装 MCP，或使用 `/mcp` 管理已有 MCP Server。
- 按 `Ctrl+C`，请求 Agent 在安全点中断当前任务。
