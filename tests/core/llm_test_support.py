from core.context import ContextBudget, ModelBudgetConfig, TiktokenTokenEstimator
from core.model_call import LLMCallResult, LLMResponse, LLMUsage
from core.model_call.service import ModelCallService
from core.runtime_artifacts import (
    ArtifactChunk,
    ArtifactNotFoundError,
    ArtifactOffsetOutOfRangeError,
    ArtifactRef,
    ToolResultExternalizer,
    ToolResultLimit,
)
from core.tool_registry import BUILTIN_TOOL_REGISTRY
from core.tools_runtime.tools_executor import ToolsExecutor
from tools.artifact_read import create_read_artifact_spec


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.contents: dict[str, str] = {}
        self._next_id = 0

    def save(self, content: str) -> ArtifactRef:
        self._next_id += 1
        artifact_id = f"art_{self._next_id:032x}"
        self.contents[artifact_id] = content
        return ArtifactRef(artifact_id, len(content))

    def read(self, artifact_id: str, offset: int, limit: int) -> ArtifactChunk:
        if artifact_id not in self.contents:
            raise ArtifactNotFoundError(artifact_id)
        content = self.contents[artifact_id]
        if offset > len(content):
            raise ArtifactOffsetOutOfRangeError(
                f"offset={offset}, total_chars={len(content)}"
            )
        end = min(offset + limit, len(content))
        return ArtifactChunk(
            artifact_id=artifact_id,
            content=content[offset:end],
            offset=offset,
            next_offset=end if end < len(content) else None,
            total_chars=len(content),
        )


def runtime_tool_dependencies(execute_result: dict | None = None) -> dict:
    store = MemoryArtifactStore()
    registry = BUILTIN_TOOL_REGISTRY.clone()
    registry.register(create_read_artifact_spec(store))
    executor = ToolsExecutor(registry)
    if execute_result is not None:
        executor.execute = lambda _name, _arguments: execute_result
    return {
        "tools_executor": executor,
        "tool_result_externalizer": ToolResultExternalizer(
            store,
            ToolResultLimit(),
        ),
    }


def call_result(
    response: LLMResponse,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> LLMCallResult:
    return LLMCallResult(
        response=response,
        usage=LLMUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


def model_call_service(llm_client) -> ModelCallService:
    return ModelCallService(
        llm_client=llm_client,
        context_budget=ContextBudget(
            estimator=TiktokenTokenEstimator(),
            config=ModelBudgetConfig(
                context_limit=10_000_000,
                input_ratio=0.75,
            ),
        ),
    )
