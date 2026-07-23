import unittest

from core.context.compactor import (
    ContextCompactor,
    ContextCompressionNotImplementedError,
    is_context_limit_error,
)


class ContextCompactorTest(unittest.TestCase):
    def test_context_limit_error_detection(self):
        self.assertTrue(is_context_limit_error("maximum context length exceeded"))
        self.assertTrue(is_context_limit_error("Input is too long for this model"))
        self.assertFalse(is_context_limit_error("connection timed out"))

    def test_compactor_is_phase_1_placeholder(self):
        with self.assertRaises(ContextCompressionNotImplementedError):
            ContextCompactor().compact([])


if __name__ == "__main__":
    unittest.main()
