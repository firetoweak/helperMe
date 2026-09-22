from __future__ import annotations

from helperme.tools.control import ControlApprovalProposal, ControlPreparationFailure


def runtime_tool_result(result: object) -> object:
    """Translate a product tool result without interpreting its domain value."""

    if isinstance(result, (ControlApprovalProposal, ControlPreparationFailure)):
        action = result.action if isinstance(result, ControlApprovalProposal) else "preparation"
        return {
            "ok": False,
            "code": "HOST_CONTROL_PLANE",
            "error": (
                "this action belongs on the host control plane: "
                f"{action}"
            ),
            "hint": "使用 /mcp 或 /skill 管理能力；安装审批不是 Runtime 工具结果。",
        }
    return result
