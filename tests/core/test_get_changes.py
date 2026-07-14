import unittest
from unittest.mock import Mock, patch

from tools.get_changes import GetChangesInput, get_changes


class GetChangesEarlyFailTest(unittest.TestCase):
    @patch("tools.get_changes.subprocess.run")
    def test_non_git_workspace_reports_verification_failure(self, run):
        run.return_value = Mock(returncode=128)

        result = get_changes(GetChangesInput())

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "VERIFICATION_BACKEND_UNAVAILABLE")
        self.assertIsNone(result["changed"])


if __name__ == "__main__":
    unittest.main()
