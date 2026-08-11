import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import Mock

from console_chat import (
    _format_token_limit,
    _latest_input_tokens,
    main,
)


class ConsoleRunMetadataTest(unittest.TestCase):
    def test_run_hyperparameters_are_not_cli_options(self):
        for arguments in (["--max-rounds", "80"], ["--full-access"]):
            with self.subTest(arguments=arguments):
                with redirect_stderr(StringIO()), self.assertRaises(
                    SystemExit
                ):
                    main(arguments)

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
