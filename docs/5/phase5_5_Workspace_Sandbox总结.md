## Phase 5.5 Workspace Sandbox 总结（完成于 2026.08.05）

目标：把「根目录相对路径解析」升级为可配置的轻量路径沙箱；不引入进程/容器隔离。

核心结论：Workspace Sandbox 是文件工具的路径权限边界，不是文件操作门面。它只回答「该逻辑相对路径在当前 root 内对应哪个安全绝对路径」；路径是否存在、类型是否符合以及是否创建父目录，仍由具体文件工具负责。

### 职责边界

```text
Composition Root
├─ 接收可信的 workspace_roots 配置
├─ 为每个 root 创建独立 WorkspaceSandbox
└─ 创建并注册绑定 WorkspaceSandboxes 的 Workspace ToolSpec
        ↓
WorkspaceSandboxes
├─ 按逻辑 root 名称选择 Sandbox
└─ 不参与单根路径解析
        ↓
WorkspaceSandbox
├─ 只接受 root 内相对路径
├─ 规范化路径并解析已有符号链接
└─ 拒绝绝对路径与 root 外逃逸
        ↓
Workspace Tools
└─ 检查存在性/文件类型并执行读取、写入、搜索或验证
```

### 路径契约

- 工具输入采用 `root + path`；`root` 是逻辑根名称，`path` 永远是该 root 内的相对路径。
- 即使绝对路径实际位于 root 内，也返回 `ABSOLUTE_PATH_NOT_ALLOWED`，不让 `path` 同时具有逻辑路径和物理路径两套语义。
- `.`、`..` 会先规范化；规范化后仍在 root 内则允许，越出 root 返回 `PATH_OUTSIDE_WORKSPACE`。
- 路径不存在不等于路径不安全；Sandbox 可以返回安全但尚不存在的路径，存在性由具体工具检查。
- 已有父路径中的符号链接会参与解析；最终目标落在 root 外时拒绝访问。
- 未知 root 由多根选择层返回 `UNKNOWN_WORKSPACE_ROOT`，单根 Sandbox 不感知其他 root。

### 错误契约

- 可预期的外部路径违规使用 `WorkspaceInputError` 领域异常表达，只在 Workspace 工具边界转换成 Tool Protocol 错误结果。
- 已明确建模的外部文件系统错误在具体工具边界转换，例如 Host 范围遍历中的拒绝访问；未建模的内部错误不统一包装、不静默兜底，保留原始异常直接失败。
- `must_exist`、`expect`、`create_parents` 已从 Sandbox 移出，分别由需要它们的具体文件工具处理。

### 应用级访问模式（2026.08.11 回补）

外部配置 `model_config.yaml` 在创建 Application 前选择不可变 FilesystemAccessMode；未来 GUI 按钮修改该配置语义并重建 Application，而不是绕过统一配置入口：

- `scoped`：默认模式，只注册显式配置的项目 roots。
- `host`：保留项目 roots，并额外发现应用启动时已挂载的本机文件系统 roots；Windows 使用 `drive_c`、`drive_d` 等稳定逻辑名称。

工具仍统一使用 `root + relative path`，不增加绝对路径分支。Host 只表示文件工具不再施加项目范围上限，实际访问继续服从当前进程的操作系统权限。模型、Plugin、Skill 和单次 Run 无权切换或升级该模式；GUI 若切换按钮，必须重新创建 Application 与 Session。

`execute_command` 当前仍可使用绝对路径访问 Workspace 外部资源，因此 scoped 是“文件工具访问范围”，不是操作系统安全隔离；在命令执行器具备真实权限隔离前，界面不得把 scoped 宣称为安全沙箱。

### 目录隔离

- Agent Source 是程序资源，正常运行时不注册为 Workspace root。
- Runtime Root 保存 Session Artifact 等内部状态，不通过普通 Workspace 工具暴露。
- User Workspace 才会作为具名 root 注入 `glob/read_file/grep/write_file/apply_patch/replace_all/get_changes` 等文件工具。
- Console 入口从 `model_config.yaml` 读取 User Workspace；测试入口显式注入临时 root，不改变 Sandbox 契约。

### 验收

- 相对路径、规范化后仍在 root 内的路径及安全的不存在路径可以解析。
- 绝对路径、`..` 越界、未知 root 与指向 root 外的符号链接会被拒绝。
- Workspace 工具不再通过全局 `WORKSPACE` 和导入副作用绑定目录，而由 Composition Root 生成并注册已绑定依赖的 ToolSpec。
- 多个 root 各自持有独立 Sandbox；同名文件必须通过 root 名称区分。
- 完整测试通过；Windows 当前环境无符号链接创建权限时，对应真实符号链接用例跳过。

### 第一版明确不做

- 进程、容器或操作系统级文件系统隔离。
- 路径校验完成后抵御并发替换的 TOCTOU 防护。
- root 级只读/读写权限模型。
- scoped 模式不自动发现 Agent Source、Runtime Root 或其他 User root；Host 模式只发现已挂载文件系统 root，显式 root 名称冲突会拒绝装配。
- GUI 用户目录选择器及其配置写回交互。
