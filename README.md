# helperMe

事件为唯一事实、状态可完整重放的个人通用助手

> [!WARNING]
> 项目仍在积极开发中。接口、配置、存储格式和已有数据都可能发生不兼容变更，升级前请自行备份需要保留的数据。

## 一个由事实驱动的 Agent Runtime

HelperMe 的核心是一条可持久化、可恢复、可追溯的执行循环：

```text
Event → State → Step → Command → Outcome → Event
```

运行中发生的一切都先成为 Journal 中不可原地修改的 Event。State 不是另一份可变数据，而是这些事实的确定性归约结果。模型每次只推进一个 Step；需要影响外部世界时提交 Command，执行结果作为新的 Event 回到下一轮决策。

**Journal 是唯一的执行事实源。** 模型上下文、摘要和诊断视图都只是投影，可以丢弃和重建，不能反过来改写事实。

这套设计直接带来四个能力：

- **完整重放**：整条 Event 流可以从头归约，能在任意历史切面重建当时的 State；恢复不依赖进程内缓存。
- **因果追溯**：一次决策看到了哪些事实、发出了哪些 Command、得到了什么 Outcome，都能沿 Journal 还原，不靠日志猜测。
- **诚实恢复**：进程重启后只从已提交事实继续。已经开始却没有结果的外部操作保持“未知”，不会被伪装成未执行或盲目重试。
- **投影可重建**：模型上下文、Trace 和未来的摘要都可以随时重新生成，优化或损坏投影不会改变真实执行历史。

**Runtime 不替模型理解世界。** 它只负责归约、调度、不变量和安全边界；目标是否满足、事实意味着什么、下一步做什么，始终交给模型、显式 Judge 或用户判断。

MCP、Skill、SubAgent 等能力也不会侵入 Runtime。它们各自通过窄边界接入，并按需进入模型上下文，使助手不断增长时，基础执行内核仍然保持稳定和可理解。

完整设计见[架构总览](docs/架构/总览.md)和[Runtime](docs/架构/Runtime.md)。

## 运行

开发和运行环境统一使用 Python 3.13.x，目前主要在 Windows 上开发和测试。

内置 `grep` 工具依赖外部 [ripgrep](https://github.com/BurntSushi/ripgrep#installation)（命令名 `rg`），需要单独安装，`requirements.txt` 不会安装它。Windows 可使用：

```powershell
winget install --id BurntSushi.ripgrep.MSVC --exact
```

安装后重新打开终端，执行 `rg --version` 验证。确保 `rg` 所在目录已加入本机用户的 `PATH`，让 HelperMe Worker 能找到它；已运行的 HelperMe 需重新启动。缺少该依赖时，调用 `grep` 会返回 `RG_NOT_FOUND`。

安装 Python 3.13 后，创建虚拟环境并启动：

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python console_chat.py
```

如果已有 `.venv` 是用其他 Python 版本创建的，先移走旧环境，再按上述命令重建；安装新版本 Python 不会自动升级已有虚拟环境。激活后可用 `python --version` 确认版本为 3.13.x。

首次启动会创建 `~/.helperme/config.json`。按[模型配置指南](docs/模型配置.md)填写模型接口和工作区后重新启动，完整配置结构见 [config.example.json](config.example.json)。

Web 入口只有一个：`python web_chat.py`，浏览器打开 `http://127.0.0.1:8765`。后端同时提供 API 和已构建的页面。

前端需要 Node.js。第一次使用或改过 `web/` 之后，先构建再启动：

```powershell
cd web
npm install
npm run build
cd ..
python web_chat.py
```

改前端时如果要用 Vite 热更新，先让上面的 Python 进程保持运行，再另开终端：

```powershell
cd web
npm run dev
```

然后打开 Vite 打印的本地地址。不要只开 Vite：页面会把 `/api` 转到 `127.0.0.1:8765`，后端不在就会一直报错。

## 文档

- [文档索引](docs/README.md)
- [项目架构方向](docs/项目架构方向.md)
- [架构总览](docs/架构/总览.md)
- [Runtime](docs/架构/Runtime.md)
- [计划](docs/计划.md)
