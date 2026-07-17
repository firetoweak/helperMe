from core.context import (
    ContextBudget,
    ContextComposition,
    ContextManager,
    ContextPreparationService,
    MicroCompactionConfig,
    MicroCompactionPolicy,
    ModelBudgetConfig,
    TiktokenTokenEstimator,
)
from core.context.composition import (
    ROLE_KEYS,
    empty_role_counts,
    empty_role_tokens,
)
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


def context_preparation_service(
    context_manager: ContextManager | None = None,
    *,
    recent_protection_tokens: int = 8_000,
    artifact_store: MemoryArtifactStore | None = None,
) -> ContextPreparationService:
    manager = context_manager or ContextManager()
    budget = ContextBudget(
        estimator=TiktokenTokenEstimator(),
        config=ModelBudgetConfig(
            context_limit=10_000_000,
            input_ratio=0.75,
        ),
    )
    summary_generator = MockSummaryGenerator()
    store = artifact_store or MemoryArtifactStore()
    return ContextPreparationService(
        context_manager=manager,
        micro_compaction_policy=MicroCompactionPolicy(
            context_manager=manager,
            context_budget=budget,
            config=MicroCompactionConfig(
                recent_protection_tokens=recent_protection_tokens,
            ),
            artifact_store=store,
        ),
        context_budget=budget,
        summary_generator=summary_generator,
    )


class MockSummaryGenerator:
    def generate(self, model_context):
        raise AssertionError("测试未预期执行 Level 2")


class CharacterEstimator:
    """按字符近似估算，供压缩/准备测试使用。"""

    def estimate(self, model_context, tools) -> int:
        return sum(
            len(str(message.get("content", "")))
            for message in model_context.messages
        ) + len(str(tools))

    def breakdown(
        self,
        model_context,
        tools,
        *,
        input_budget_tokens: int,
    ) -> ContextComposition:
        by_role = empty_role_tokens()
        counts = empty_role_counts()
        tool_result_chars = 0
        for message in model_context.messages:
            role = message.get("role")
            if role not in ROLE_KEYS:
                role = "assistant"
            size = len(str(message.get("content", "")))
            by_role[role] += size
            counts[role] += 1
            if role == "tool":
                tool_result_chars += size
        tools_schema = len(str(tools))
        total = self.estimate(model_context, tools)
        parts = sum(by_role.values()) + tools_schema
        if parts != total:
            by_role["assistant"] += total - parts
        return ContextComposition(
            estimated_total_tokens=total,
            input_budget_tokens=input_budget_tokens,
            tools_schema_tokens=tools_schema,
            by_role_tokens=by_role,
            by_role_message_counts=counts,
            tool_result_chars=tool_result_chars,
        )

    def calibrate(self, model_context, tools, actual_input_tokens) -> None:
        return None
