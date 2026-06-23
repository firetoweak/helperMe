# agent 的调用核心 loop循环 
import json
from datetime import datetime
from pathlib import Path

from core.messages import Conversation
from core.llm_client import LLMClient
# 注册
import tools
from core.tool_registry import get_tools
from core.tools_executor import execute_tool

RUN_LOG = Path(__file__).resolve().parent.parent / "run.log"


def _format_message(msg: dict) -> str:
    role = msg["role"]
    if msg.get("tool_calls"):
        body = json.dumps(msg["tool_calls"], ensure_ascii=False, indent=2)
        return f"{role}:\n{body}"
    content = msg.get("content")
    if content is None:
        return f"{role}: (no content)"
    return f"{role}: {content}"


def _write_run_log(lines: list[str]) -> None:
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


class Agent:
    def __init__(self, model: str = "default"):
        self.llm_client = LLMClient()
        self.conversation = Conversation()
        self.model = model

    def run(self, user_message: str, max_rounds: int = 10):

        self.conversation.add_user(user_message)
        for _ in range(max_rounds):
            response = self.llm_client.chat(
                self.conversation.messages, 
                self.model, 
                get_tools())

            self.conversation.add_assistant(response)
            if response.type == "text":
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
    # agent.conversation.set_system_prompt(
    #     "你是一个助手。"
    #     "需要日期时调用 get_today_date；需要计算时调用 add。"
    #     "不要自己编造，要用工具。"
    # )
    # print("=== 测试 get_today_date ===")
    # print(agent.run("今天是几号？"))
    # agent.conversation.reset()
    agent.conversation.set_system_prompt(
        "你是一个智能体助手，你可以帮助用户分析问题，并给出解决方案。用户可能提出模糊的请求。"
        "你需要根据用户提问和上下文，选择最合适的工具来解决问题。"
        "问题涉及到关键文件内容的话，必须读取关键实现文件，信息不足时继续调用工具。"
        "工具对于用户的提问来说是隐藏的。"
    )
    print("\n=== 测试 工具集合 ===")
    question = "[用户提问] 分析这个项目的启动入口、主要模块和 Agent Loop 调用链。"
    answer = agent.run(question)

    log_lines = [
        f"=== run @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===",
        f"Q: {question}",
        f"A: {answer}",
        "--- trace ---",
    ]
    for i, msg in enumerate(agent.conversation.messages):
        log_lines.append(f"  [{i}] {_format_message(msg)}")
    _write_run_log(log_lines)

    print(answer)
    print(f"(完整日志已写入 {RUN_LOG})")
