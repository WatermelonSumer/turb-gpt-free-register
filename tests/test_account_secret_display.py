import json
import unittest
from unittest.mock import patch

from webui.app import _compact_account_for_list, create_app


class AccountSecretDisplayTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="test-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_compact_account_only_exposes_password_flag_and_access_token_tail(self):
        password = "Password!1234"
        access_token = "access-token-12345678"
        compact = _compact_account_for_list({
            "id": 7,
            "email": "user@example.test",
            "access_token": access_token,
            "extra_json": json.dumps({"registration_password": password}),
        })

        self.assertTrue(compact["has_password"])
        self.assertTrue(compact["has_access_token"])
        self.assertEqual(compact["access_token_tail"], "12345678")
        self.assertNotIn("registration_password", compact)
        self.assertNotIn("extra_json", compact)
        serialized = json.dumps(compact)
        self.assertNotIn(password, serialized)
        self.assertNotIn(access_token, serialized)

    def test_compact_account_reports_missing_password_and_access_token(self):
        compact = _compact_account_for_list({"id": 8, "email": "empty@example.test"})

        self.assertFalse(compact["has_password"])
        self.assertFalse(compact["has_access_token"])
        self.assertEqual(compact["access_token_tail"], "")

    def test_password_secret_endpoint_returns_full_registration_password(self):
        account = {
            "id": 7,
            "email": "user@example.test",
            "extra_json": json.dumps({"registration_password": "Password!1234"}),
        }
        with patch("webui.app.db.get_account", return_value=account):
            response = self.client.get(
                "/api/accounts/7/secret?field=password",
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["value"], "Password!1234")

    def test_access_token_secret_endpoint_still_returns_full_value(self):
        account = {"id": 7, "email": "user@example.test", "access_token": "full-access-token"}
        with patch("webui.app.db.get_account", return_value=account):
            response = self.client.get(
                "/api/accounts/7/secret?field=access_token",
                headers=self.headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["value"], "full-access-token")


if __name__ == "__main__":
    unittest.main()
