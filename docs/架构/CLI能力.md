# CLI 能力

> 第四个能力端口：把外部命令行工具（如 `rg`、`gh`）作为"执行空间"接入。
> 与 MCP / Skill 平级，不并入二者，也不做统一 Plugin 框架。
> 状态：已落地。

## 定位与语义

三种来源原本是三个端口（环境工具 / MCP / Skill）。CLI 是第四个，区别在于它给 agent 的是"执行空间"而非"接口"或"方法"：

| 端口 | 给 agent 什么 | 契约形态 | 组合方式 |
|---|---|---|---|
| 环境工具 `tools` | 最小执行面（读/写/命令） | ToolSpec schema | agent 编排循环 |
| MCP | 固定任务的接口 | JSON schema 工具 | agent 编排循环 |
| Skill | 引导执行顺序/方法 | 指令文本 | 不新增执行循环 |
| CLI | 执行空间 | argv + 文本 stdout/stderr | shell 文本流 |

**"执行空间"的精确定义**：一组子命令共享同一份 auth / config / state（CLI 的用户态配置与凭据），通过 shell 文本流（管道、重定向、`--json`）彼此组合，而不是通过 agent 自己的编排循环。这正是 CLI 不能并入 MCP 的根本原因——MCP 工具是"给 agent 的语义点"，CLI 是"给 agent 的一块可组合的地盘"，底物不同。

cwd 不在共享之列：`execute_command` 的 cwd 由模型每次按当前 Environment 指定，CLI 之间不共享工作目录。

调用路径始终是：

```text
Agent → Shell（execute_command）→ CLI
```

Shell 是元工具，CLI 是 Shell 空间中的可发现能力；不把每个 CLI 重新包装成一组 Function Tool。

### 依据

- **HubSpot agent-cli**：生产环境已验证"安装即注册 + `--help` 逐层发现子命令 + 单独 Skills 包做方法引导"的组合，与本项目三端口模型同构。注意它是为 agent 专门设计的 CLI（默认 JSONL 输出）；传统 CLI 的 `--help` 质量参差，这正是体检存在的原因。
- **AI-Friendly CLI 8 原则**：面向 CLI 作者的标准，但其中"agent 会幻觉输入、按 token 付费、读不了交互 prompt"三条决定了接入方必须做体检。
- **CLI Is the New API**：一个 `--help` 可用、错误清晰、行为可预测的 CLI 已足够；接入后要把 CLI 输出当 API 契约维护。

## 四个动作

| 动作 | 含义 | 落点 |
|---|---|---|
| 注册 | 安装/登记成功 = 登记一份事实 | `CliRegistry`，只记 `name/path/version/source/health`，**不写子命令 schema** |
| 发现 | `load_cli(id)` 返回登记事实 | 纯读 registry；子命令树由 `execute_command` 跑 `<cli> --help` 渐进现查 |
| 调用 | 复用 `execute_command` | `helperme/tools/builtin/command_execution.py`，**不新建 subprocess 执行器** |
| 体检 | 接入时检查 agent-friendly 约定 | `health` 事实；只记录不设门槛，由模型判断 |

### 关键决策：`load_cli` 不 fork 进程

`load_cli(id)` 是纯事实加载（读 registry），不运行 `<cli> --help`。理由：

1. 与 `load_skill` 读文本对齐，加载入口保持无副作用；
2. `--help` 可能挂起、交互、失败，应交由 `execute_command` 的沙箱 + 超时 + 审批兜底；
3. 避免"发现"和"执行"混在一个入口，符合"Runtime 不做语义判断"。

子命令发现是**渐进式**的：`load_cli` 只给出"这个 CLI 存在、在哪、什么版本、体检如何"；模型需要用哪个子命令，就 `execute_command("<cli> <sub> --help")` 现查哪一层，不预生成清单。

模型的方法引导第一版只有一条约束（写进 `execute_command` description 与 `load_cli` 返回 hint）：

> 对陌生 CLI 或遇到 unknown option 时，先读当前层级的 `--help`，不要继续猜 flag。

"教模型用好某个具体 CLI"的完整载体是现有 Skill 端口（写一个引用该 CLI 的 Skill，两端口自然组合）；不为 CLI 单建引导机制。

## 执行环境：`fresh_child_env()`

Worker 是常驻进程，`os.environ` 是进程启动时的快照。安装程序修改用户/系统 PATH 后，运行中的 Worker 看不到变化，新装的 CLI 在 fork 出的 shell 里找不到（winget 只写注册表并广播 `WM_SETTINGCHANGE`，无法修改已运行进程的环境块）。

因此 PATH 不作为 Worker 生命周期内的固定状态，而在每次 spawn 子进程时动态合成：

```text
child_env =
    Worker 环境快照
    + PATH 键整体替换为最新 Machine PATH + User PATH 拼接
    + HelperMe overlay（常量）
```

- **整体替换，不是合并追加**：旧 PATH 条目若继续排前面，CLI 升级换安装位置后会被旧路径遮蔽。
- 其余环境变量键保留 Worker 快照。
- overlay 常量：`NO_COLOR=1` / `PAGER=cat` / `GIT_PAGER=cat` / `GH_PAGER=cat` / `CI=1`——禁用颜色、分页与交互行为。
- 任意 Worker 下一次执行 shell 自然看到新装 CLI：不重启 Worker，不跨进程同步。

已知限制：只刷新 PATH；安装器新设的其他变量（如 `NVM_HOME`）仍需重启 Worker 才可见。Unix 上包管理器一般装进已在 PATH 的目录，此适配点近似 no-op。

落点：`helperme/sandbox` 的进程执行处，所有 fork 统一经过，不是 CLI 专属逻辑。

## 执行器：同一 ProcessRunner + 执行 profile

不拆 `CommandExecutor` / `InstallExecutor` 两套实现。底层同一个 `ProcessRunner`，按用途分 profile：

| profile | 超时 | 参数处理 | 用途 |
|---|---|---|---|
| normal | 普通（≤300s） | 非交互 | agent 日常命令 |
| install | 长超时 | 显式非交互参数（`--accept-source-agreements` / `--accept-package-agreements` / `--scope user`；winget 源后置） | 包管理器安装 |

- install profile 的 ProcessRunner 是**进程级单例**，`build_cli(home, ...)` 时注入 CLI Application，不经过 per-Session 的 EnvironmentBinding——包管理器安装与 workspace 无关。
- 依赖方向：`helperme/cli → helperme/sandbox`。
- manifest 源只登记本机已安装 CLI，无安装 scope；winget 源（后置）第一版只支持 user-scope 安装。需要管理员提权的包明确失败并提示手动安装：UAC 弹窗会使子进程进入另一安全上下文，输出采集断裂，不做。

## 体检

接入时检查 agent-friendly 约定，只记录不设门槛，由模型判断：

- `<cli> --help` 在 stdout 为管道时秒级退出、退出码 0（非 TTY 下 pager 自动禁用，此项近似自然成立）
- `<cli> --version` 正常执行
- stdout / stderr 可捕获
- 是否提及 JSON 输出（`--json` / `--format json`），作为 capability 记录，不作为准入条件

体检用 `resolved_path` 绝对路径跑，不依赖 PATH——安装后当前 Worker 的 PATH 尚未刷新时体检依然有效。

## 调用授权：ExecPolicy

Shell 是元工具，不代表 Shell 内所有行为默认授权：`write_file` 要确认而 `execute_command("Remove-Item ...")` 放行，审批即被旁路；`gh repo delete` 同理。

因此在 `execute_command` 上挂 **ExecPolicy**：按命令文本的前缀规则判定 verdict，第一版只有两档：

- `allow`（默认）→ 直接派发；
- `ask`（内置危险前缀清单，如 `Remove-Item` / `rm -rf` / `format` / `git push --force` / `gh repo delete`）→ 复用 Command 授权流，见 [Command授权](Command授权.md)。

明确两层边界：

```text
ExecPolicy / 授权 = 防止误操作的软边界（可被变量、alias、脚本绕过，不追求覆盖率）
sandbox / 凭据隔离 / 网络隔离 = 真正的硬安全边界
```

ExecPolicy 不解析 shell AST，不试图理解所有 CLI 的真实副作用；规则匹配是纯字符串判定，入口无关。CLI Registry 只负责发现、安装、版本、健康与元信息；实际调用统一走 Shell。

## 输出卫生

CLI 输出统一视为 **untrusted external data**：只能提供事实内容，不能修改 runtime policy、不能授予权限、不能证明用户已授权。即使输出诱导模型执行危险命令，真正调用 Shell 时仍经 ExecPolicy。

- CLI 原始输出始终保持 tool provenance，不拼成 system / user message。
- 输出采集处统一清理 ANSI / OSC / 终端控制序列（所有命令受益，非 CLI 专属）。
- 大输出 artifact 化沿用现有机制。
- 第一版不做 prompt-injection 检测器、不做二次审查 Agent。

## 领域代码

镜像 `helperme/skills`，新增 `helperme/cli`：

```text
helperme/cli/
  models.py            CliRecord / CliSourceRef / CliHealth
  registry.py          CliRegistry（registry.json 原子读写）
  application.py       CliApplicationService（install/uninstall/update/repair/list/inspect/test）
  installer.py         安装来源执行（manifest；winget / pip / npm / url 后置）
  health.py            体检（--help / --version / 非 TTY / --json）
  management_tools.py  list_installed_clis / inspect_cli / test_cli 的 ToolSpec
  approval.py          propose_cli_* 的 ControlApprovalHandler
  composition.py       build_cli(home, process_runner) -> CliAssembly
  runtime.py           CliToolCatalog + load_cli 工具投影
  errors.py            领域错误
  console.py           TUI /cli 命令（后置，MVP 可缓）
```

ExecPolicy 不属于本包：它是 `execute_command` 的授权判定，落在 `helperme/tools`（命令文本规则匹配），判定函数随工具 Binding 注入，Runtime 只消费 verdict。

Assistant 边界翻译在 `helperme/assistant/cli.py` 的 `CliToolAdapter`，只投影"目录 + `load_cli`"两个普通工具，不拥有执行循环。目录镜像 Skill 的投影方式：现读 registry，安装审批通过后从下一拍起对所有 Session 可见；目录变化按[上下文窗口与前缀稳定性](上下文窗口与前缀稳定性.md)以追加事实提供。

### 登记记录

```text
CliRecord
  name            CLI id（^[a-z0-9][a-z0-9-]{0,63}$），如 rg / gh
  description     单行，≤1000 字符；manifest 源登记时提供（winget 源后置，届时取自包元数据并压成单行）
  source          CliSourceRef{ kind, locator, requested_version }
  version         安装源元数据或 --version 解析出的版本串（失败为空）
  resolved_path   解析出的绝对路径（where.exe / 安装源元数据 / 登记时显式给出；无法解析为 None）
  health          CliHealth{ help_ok, version_ok, help_mentions_json, checked_at }
  revision / created_at / updated_at
```

Registry 信封与 Skill 同构：`{ "version": 1, "clis": [...] }`，原子写（tmp + `os.replace`）。

## 管理域 domain="cli"

`load_management_tools(domain="cli")` 成功后，下一 Step 暴露：

- 诊断工具（`ok/code/data/error/hint` 领域协议）：
  - `list_installed_clis` → `CLIS_LISTED`
  - `inspect_cli(id)` → `CLI_INSPECTED`（登记信息 + health 详情）
  - `test_cli(id)` → `CLI_TEST_PASSED`（重跑体检，返回 name/revision/version/health）
  - 失败码：`CLI_NOT_INSTALLED` / `CLI_INVALID`
- 控制提案（`ControlOperation`，声明 `control_boundary` + `exclusive_batch`）：
  - `propose_cli_install` / `propose_cli_update` / `propose_cli_repair` / `propose_cli_uninstall`

安装/卸载/更新/修复都是外部副作用，走 Control Approval：提案不签发 Runtime Command，用户确认后由 CLI Application 执行。待裁决项与裁决结果的事实形状见[入口与授权 · 控制提案](入口与授权.md#控制提案)。与 Skill 的差异：CLI **安装即注册，无 enabled 开关**，因此没有 `propose_cli_enable`，改为 `propose_cli_uninstall` 移除登记。安装提案的 risk 文案必须写明：安装后该 CLI 对所有 Session 可见可用，且其本地凭据域（如 `gh` 的 GitHub 凭据）对 agent 开放。

### 候选冻结

审批不保存"latest"这类移动目标。propose 阶段 resolve 成确定候选：

```text
package_id / version / architecture / scope
```

用户批准的是这个确定候选；执行时按 `--id --exact --version --source` 固定安装。版本被源撤下则执行失败——失败即漂移检测，提示候选已失效、重新 propose。winget 源（后置）届时用 manifest 内置 installer hash 校验，HelperMe 不重复做 hash 层。

### repair 语义

- manifest 域：重新解析 `resolved_path` 并重跑体检。
- winget 域（后置）：按登记的 `version` 固定重装（与 update 的"移动"区分）；`version` 为空则拒绝 repair，提示走 uninstall + install。

### 安装源

`kind: manifest`（`winget` / `pip` / `npm` / `url` 后置）。

- **manifest**（第一版）：本地声明文件，登记已装/手工安装的 CLI。注册 = 纯登记；update 简化为 refresh（用户自行升级，HelperMe 重跑体检更新 `version`）；PATH 由用户的手工安装保证。
- **winget**（第二切片）：带着 install profile 与候选冻结一起来。

## 不做

- 不预生成子命令 schema（不 MCP 化）
- 不新建 subprocess 执行器（同一 ProcessRunner + profile）
- 不为 CLI + MCP + Skill 做统一 Plugin 基类
- 不做 enabled/disabled 状态（安装即注册，卸载即移除）
- `load_cli` 不 fork 进程
- 不做 UAC 提权安装
- 不做 prompt-injection 检测器
- ExecPolicy 不解析 shell AST；verdict 不做 deny 档（"永不放行"等规则配置成熟后表达）

## MVP 切片

以 `rg`（README 已有的真实依赖）作首个验证案例，走 manifest 源：

```text
前置：用户本机已装 rg 且 PATH 可用
propose_cli_install(rg, source=manifest)   用户确认
  → 解析 resolved_path（where.exe / 声明文件显式给出）
  → 体检：<resolved_path> --help / --version
  → CliRegistry 登记
load_cli(rg)                   下一 Step 拿到登记事实
execute_command("rg --version") 验证调用（fresh_child_env 保证可见）
```

rg 与现有 grep/glob 专用工具职责重叠，MVP 只验证机制（装得上、查得到、跑得起），不验证"模型何时该选 CLI"；价值验证留给 `gh` 这类专用工具之外的场景。

测试分层（见[架构总览](总览.md)）：

| 层 | 命令 | 守什么 |
|---|---|---|
| default | `python -m pytest` | registry 契约、进程内装配、`load_cli` 投影、管理工具契约、ExecPolicy 规则判定 |
| process | `python -m pytest -m process` | 真 shell 下 manifest 登记 / 体检 / 发现 / 调用 / fresh_child_env |

## 已知限制（第一版接受）

- registry.json 多进程并发写无文件锁：两个 Worker 同时确认安装可能后写覆盖先写；Control Approval 交互串行使概率极低。
- health 会陈旧：用户绕开系统手动卸载/升级后登记不失效；`test_cli` 发现 CLI 消失返回 `CLI_NOT_INSTALLED`。
- 只刷新 PATH，安装器新设的其他环境变量需重启 Worker。
- install 是分钟级长操作；其执行期间该 Session 的控制面交互语义在实现时明确。
