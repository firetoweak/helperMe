import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import Mock

from console_chat import (
    _format_token_limit,
    _handle_new_session_command,
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

    def test_new_command_creates_and_switches_to_new_session(self):
        application = Mock()
        application.create_session.return_value = "new-session-id"

        session_id = _handle_new_session_command(application, "/new")

        self.assertEqual(session_id, "new-session-id")
        application.create_session.assert_called_once()
        created_session_id = application.create_session.call_args.args[0]
        self.assertTrue(created_session_id.startswith("session-"))

    def test_other_input_is_not_handled_as_new_command(self):
        application = Mock()

        session_id = _handle_new_session_command(application, "/new task")

        self.assertIsNone(session_id)
        application.create_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
