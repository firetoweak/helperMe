import json
from pydantic import ValidationError
from tool_registry import TOOL_SPECS



def execute_tool(tool_name: str, tool_arguments: str) -> str:
    spec = TOOL_SPECS[tool_name]

    if spec is None:
        return json.dumps({
            "error": f"Tool {tool_name} not found"
        })

    try:
        payload = json.loads(tool_arguments or "{}")
        data = spec.input_model.model_validate(payload)
        result = spec.handler(data)
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"invalid json: {exc}"}, ensure_ascii=False)
    except ValidationError as exc:
        return json.dumps({"error": exc.errors()}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


    