from core.context import ContextBudget, ModelBudgetConfig, TiktokenTokenEstimator
from core.model_call import LLMCallResult, LLMResponse, LLMUsage
from core.model_call.service import ModelCallService


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
