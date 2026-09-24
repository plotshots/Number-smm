import os
import unittest
from unittest.mock import patch

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test")
os.environ.setdefault("MONGODB_URI", "mongomock://imap-credentials")

from utils.imap_verifier import get_imap_credentials


class ImapCredentialTests(unittest.TestCase):
    def test_credentials_are_read_from_environment(self):
        with patch.dict(os.environ, {
            "GMAIL_USERNAME": "configured@example.com",
            "GMAIL_APP_PASSWORD": "app-password-value",
        }, clear=False):
            self.assertEqual(
                get_imap_credentials(),
                ("configured@example.com", "app-password-value"),
            )

    def test_missing_credentials_fail_safely_without_logging_values(self):
        with patch.dict(os.environ, {
            "GMAIL_USERNAME": "",
            "GMAIL_APP_PASSWORD": "secret-that-must-not-log",
        }, clear=False):
            with self.assertLogs("config", level="ERROR") as captured:
                self.assertEqual(get_imap_credentials(), ("", ""))
        output = "\n".join(captured.output)
        self.assertIn("GMAIL_USERNAME", output)
        self.assertNotIn("secret-that-must-not-log", output)

    def test_empty_app_password_fails_safely(self):
        with patch.dict(os.environ, {
            "GMAIL_USERNAME": "configured@example.com",
            "GMAIL_APP_PASSWORD": "",
        }, clear=False):
            self.assertEqual(get_imap_credentials(), ("", ""))


if __name__ == "__main__":
    unittest.main()