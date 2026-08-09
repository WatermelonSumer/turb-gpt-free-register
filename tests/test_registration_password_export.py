import json
import unittest

from core import account_export, db


class RegistrationPasswordExportTests(unittest.TestCase):
    def test_account_line_uses_account_password_totp_access_token_order(self):
        line = db._account_line({
            "email": "user@example.test",
            "access_token": "access-token",
            "totp_secret": "TOTPSECRET",
            "extra_json": json.dumps({"registration_password": "Password!1234"}),
        })
        self.assertEqual(
            line,
            "user@example.test----Password!1234----TOTPSECRET----access-token",
        )

    def test_account_line_keeps_empty_columns(self):
        line = db._account_line({
            "email": "user@example.test",
            "access_token": "access-token",
            "extra_json": json.dumps({"registration_password": "Password!1234"}),
        })
        self.assertEqual(
            line,
            "user@example.test----Password!1234--------access-token",
        )

    def test_account_line_without_registration_password_keeps_fixed_columns(self):
        self.assertEqual(
            db._account_line({
                "email": "user@example.test",
                "access_token": "access-token",
                "totp_secret": "TOTPSECRET",
            }),
            "user@example.test--------TOTPSECRET----access-token",
        )
        self.assertEqual(
            db._account_line({
                "email": "user@example.test",
                "access_token": "access-token",
            }),
            "user@example.test------------access-token",
        )

    def test_account_line_does_not_expose_original_email_material(self):
        self.assertEqual(
            db._account_line({
                "email": "user@example.test",
                "original_email_line": "user@example.test----mail-password----client----refresh",
                "access_token": "access-token",
                "extra_json": json.dumps({"registration_password": "Password!1234"}),
            }),
            "user@example.test----Password!1234--------access-token",
        )

    def test_account_line_ignores_invalid_extra_json(self):
        self.assertEqual(
            db._account_line({
                "email": "user@example.test",
                "access_token": "access-token",
                "extra_json": "{invalid",
            }),
            "user@example.test------------access-token",
        )

    def test_batch_copy_line_uses_same_password_column(self):
        self.assertEqual(
            account_export._account_copy_line(
                "user@example.test",
                "access-token",
                registration_password="Password!1234",
            ),
            "user@example.test----Password!1234--------access-token",
        )


if __name__ == "__main__":
    unittest.main()
