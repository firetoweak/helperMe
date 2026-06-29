# agent 的调用核心 loop循环 
import json
import os
from datetime import datetime
from pathlib import Path

from core.messages import Conversation
from core.llm_client import LLMClient
# 注册
import tools
from core.tool_registry import get_tools
from core.tools_executor import execute_tool

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def get_default_run_log_path() -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return PROJECT_ROOT / f"run_{today}.log"


def _format_message(msg: dict) -> str:
    role = msg["role"]
    if msg.get("tool_calls"):
        body = json.dumps(msg["tool_calls"], ensure_ascii=False, indent=2)
        return f"{role}:\n{body}"
    content = msg.get("content")
    if content is None:
        return f"{role}: (no content)"
    return f"{role}: {content}"


def _write_run_log(lines: list[str], path: Path | None = None) -> None:
    log_path = path if path is not None else get_default_run_log_path()
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class Agent:
    def __init__(self, model: str = "default"):
        self.llm_client = LLMClient()
        self.conversation = Conversation()
        self.model = model

    def run(self, user_message: str, max_rounds: int = 20):

        self.conversation.add_user(user_message)
        for _ in range(max_rounds):
            response = self.llm_client.chat(
                self.conversation.messages, 
                self.model, 
                get_tools())

            self.conversation.add_assistant(response)
            if response.type == "text":
                if not response.content:
                    self.conversation.add_user("你刚才返回了空内容。请继续完成任务：如果需要修改就调用工具；如果已完成就给出总结。")
                    continue
                return response.content
            else:
                # 添加工具列表
                tool_results = []
                for call in response.calls:
                    tool_result = execute_tool(call.name, call.arguments)
                    tool_results.append({
                        "tool_call_id": call.id,
                        "content": tool_result
                    })
                self.conversation.add_tools_result(tool_results)
        return "工具调用次数过多，已停止。"

if __name__ == "__main__":
    agent = Agent(model="qwen27b")
    agent.conversation.set_system_prompt(
        "你是一个智能体助手，你可以帮助用户分析问题，并给出解决方案。用户可能提出模糊的请求。"
        "你需要根据用户提问和上下文，选择最合适的工具来解决问题。"
        "问题涉及到关键文件内容的话，必须读取关键实现文件，信息不足时继续调用工具。"
        "工具对于用户的提问来说是隐藏的。"
        "`truncated=true` 时必须用 `next_offset` 续读，禁止 patch"
        "用户提问涉及到文件修改的话，完成文件修改后，必须调用 get_changes 查看实际改动。最终总结只能基于 get_changes 中真实出现的改动。"
        "如果计划修改了某处但 diff 中没有出现，必须说明“未完成”，不能声称已经修改。"
    )
    print("\n=== 测试 工具集合 ===")
    question = "[用户提问] 你觉得项目的工具描述是不是有点像一个code agent？你帮我优化一下描述，让它更像一个通用智能体。注意，只改 tools/file_read.py，我先看看结果！"
    answer = agent.run(question)

    log_lines = [
        f"=== run @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===",
        f"Q: {question}",
        f"A: {answer}",
        "--- trace ---",
    ]
    for i, msg in enumerate(agent.conversation.messages):
        log_lines.append(f"  [{i}] {_format_message(msg)}")
    log_path = (
        Path(os.environ["HELPER_RUN_LOG_PATH"])
        if "HELPER_RUN_LOG_PATH" in os.environ
        else get_default_run_log_path()
    )
    _write_run_log(log_lines, log_path)

    print(answer)
    print(f"(完整日志已写入 {log_path})")
