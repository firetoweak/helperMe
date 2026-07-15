from core.model_call import LLMCallResult, LLMResponse, LLMUsage


def call_result(response: LLMResponse) -> LLMCallResult:
    return LLMCallResult(
        response=response,
        usage=LLMUsage(input_tokens=0, output_tokens=0),
    )
