from __future__ import annotations

import unittest

from helperme.assistant.failures import assistant_failure_message
from helperme.llm.api import (
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMProviderError,
    LLMTransientError,
)


class ModelFailureMessageTest(unittest.TestCase):
    def test_authentication_error_points_to_model_configuration(self):
        message = assistant_failure_message(LLMAuthenticationError("401"))

        self.assertIn("模型认证失败", message)
        self.assertIn("model.api_key", message)

    def test_transient_error_reports_exhausted_retries(self):
        message = assistant_failure_message(LLMTransientError("timeout"))

        self.assertIn("自动重试仍未成功", message)
        self.assertIn("timeout", message)

    def test_other_known_model_errors_preserve_provider_detail(self):
        context = assistant_failure_message(LLMContextLengthError("too long"))
        provider = assistant_failure_message(LLMProviderError("bad model"))

        self.assertIn("too long", context)
        self.assertIn("bad model", provider)

    def test_unknown_internal_error_is_not_presented_as_model_failure(self):
        self.assertIsNone(assistant_failure_message(AssertionError("bug")))
