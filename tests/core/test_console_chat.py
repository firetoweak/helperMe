import unittest
from unittest.mock import Mock

from console_chat import _format_token_limit, _latest_input_tokens


class ConsoleRunMetadataTest(unittest.TestCase):
    def test_latest_input_tokens_uses_last_model_usage(self):
        outcome = Mock()
        outcome.result.checkpoints = [
            Mock(reason="llm_usage", data={"input_tokens": 12}),
            Mock(reason="llm_retry", data={}),
            Mock(reason="llm_usage", data={"input_tokens": 123}),
        ]

        self.assertEqual(_latest_input_tokens(outcome), 123)

    def test_token_limit_uses_compact_k_suffix(self):
        self.assertEqual(_format_token_limit(20_000), "20K")
        self.assertEqual(_format_token_limit(200_000), "200K")


if __name__ == "__main__":
    unittest.main()
