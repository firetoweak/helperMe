这是一个学习自主智能体的项目。

主要目标是学习，解析。

我希望AI以提问的方式更好的帮助我理解自主智能体的架构，设计逻辑。
并且，在这个项目上，我希望持续强化自己的认知学习能力，防止AI过度滥用使得自身能力退化。


## 第一期：

解决一个问题：
构建一个最简单的tools calling loop
构建一个今天日期的函数，就调用这个


基本框图：

用户输入-模型判断是直接回复还是调用工具——LLM选择调用工具——python执行工具——返回工具执行结果——LLM继续决策——输出结果


## 第二期

能碰真实文件系统 agent,需要agent自己看环境，操纵文件

**找到当前所在目录**
get_workspace_info()  

**检索目录结构**
list_files(path)

**读**
read_file(path)
1. 仅支持文本文件

**从文件中找关键字段**
search_texts(query, path)
1. 底层使用 ripgrep (rg)，python-ripgrep

**写/改**
write_file(path)


需要的：
1. 安全边界————先不做，优化时做
2. 返回格式统一 dict + json
3. System prompt 写清楚策略
4. 限制，避免 agent「瞎翻」


## 第三期

解决当前工具产生的一些bug:
1. 空响应没有单独状态，模型策略prompt不足
2. workspace 安全边界还没实现
3. search_texts 还有很大问题，agent几乎不用search


整体设计有些问题：根本没想清楚完整的file tools loop
文件处理应该按照意图来:


GROP  查看目录结构  查文件名，根据目录查文件
fd 命令 固定-g
在 workspace 内，按文件名/路径 pattern 找文件或目录，返回路径列表。
第一版不加 --hidden：默认不搜 .git 等隐藏项（和 fd 默认一致）

---

下边总结为 GREP
search_text     根据关键词找到位置
由于文件类型很多，处理方式也很多，可能需要再分工具（以后再做）
v1 不加：-i 大小写、-F 固定字符串、glob 过滤（以后再说）。

---

read_file       读文件片段

---

apply_patch     **局部**修改/新增/删除  不做整体删除

---

文件 新建  有了就可以同时支持全文重写
write_file  
delete_file     做run_command时统一做

可选：
get_diff        检查改动
run_command     执行测试/检查；带白名单的可选的human in the loop

---

优化agent做成多轮对话，并解决当前设置轮次导致失败问题

**分阶段学习计划**（打基础 → 类似 OpenClaw 的自主助手）：见 [docs/自主Agent学习计划.md](docs/自主Agent学习计划.md)


**安装工具：ripgrep，fd**