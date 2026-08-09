# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import email as email_config
from core import db, email_provider
from core.pickup_source import is_dynamic_source, pickup_domain
from webui.app import create_app


PICKUP_URL = (
    "https://mail.ai1998.xyz/messages/TOKEN/"
    "unison-libel-1a%40icloud.com"
)


class PickupDomainSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._patches = [
            patch.object(db, "_GENERIC_API_EMAIL_JSON", root / "pickup.json"),
            patch.object(db, "_GENERIC_API_EMAIL_TXT", root / "pickup.txt"),
            patch.object(db, "_ACCOUNTS_JSON", root / "accounts.json"),
            patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"),
            patch.object(db, "_TOKENS_TXT", root / "tokens.txt"),
            patch.object(db, "_VIEWER_HTML", root / "viewer.html"),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in self._patches:
            item.stop()
        self.tmp.cleanup()

    def test_pickup_url_uses_hostname_as_source(self):
        self.assertEqual(pickup_domain(PICKUP_URL), "mail.ai1998.xyz")
        self.assertTrue(is_dynamic_source("mail.ai1998.xyz"))
        self.assertEqual(
            pickup_domain("MAIL.AI1998.XYZ/messages/TOKEN/user%40icloud.com"),
            "mail.ai1998.xyz",
        )

    def test_import_groups_and_claims_by_pickup_domain(self):
        records = [
            {"email": "unison-libel-1a@icloud.com", "code_url": PICKUP_URL},
            {
                "email": "second@example.com",
                "code_url": "https://inbox.example.net/messages/TOKEN/second%40example.com",
            },
        ]
        self.assertEqual(db.import_generic_api_emails(records), (2, 0))

        summaries = db.generic_api_source_summaries()
        self.assertEqual(summaries["generic_api"]["available"], 2)
        self.assertEqual(summaries["mail.ai1998.xyz"]["available"], 1)
        self.assertEqual(summaries["inbox.example.net"]["available"], 1)

        claimed = db.claim_next_generic_api_email(source="mail.ai1998.xyz")
        self.assertEqual(claimed["email"], "unison-libel-1a@icloud.com")
        self.assertEqual(claimed["source"], "mail.ai1998.xyz")
        remaining = db.list_generic_api_email_pool(source="inbox.example.net")
        self.assertEqual([row["email"] for row in remaining], ["second@example.com"])

    def test_legacy_non_domain_pickup_stays_in_aggregate_without_double_counting(self):
        self.assertEqual(db.import_generic_api_emails([
            {"email": "legacy@example.com", "code_url": "/api/messages/legacy"},
        ]), (1, 0))

        summaries = db.generic_api_source_summaries()
        self.assertEqual(summaries["generic_api"]["available"], 1)
        self.assertEqual(summaries["generic_api"]["total"], 1)
        self.assertEqual(db.list_generic_api_sources(), [])

    def test_status_and_delete_are_scoped_to_dynamic_source(self):
        db.import_generic_api_emails([
            {"email": "unison-libel-1a@icloud.com", "code_url": PICKUP_URL},
        ])
        claimed = db.claim_next_generic_api_email(source="mail.ai1998.xyz")
        self.assertIsNotNone(claimed)
        db.release_generic_api_email(
            "unison-libel-1a@icloud.com",
            status="failed",
            source="other.example.net",
        )
        self.assertEqual(
            db.get_generic_api_email_by_email("unison-libel-1a@icloud.com")["status"],
            "used",
        )
        db.release_generic_api_email(
            "unison-libel-1a@icloud.com",
            status="available",
            source="MAIL.AI1998.XYZ.",
        )
        self.assertEqual(
            db.get_generic_api_email_by_email("unison-libel-1a@icloud.com")["status"],
            "available",
        )
        self.assertFalse(
            db.delete_generic_api_email("unison-libel-1a@icloud.com", source="other.example.net")
        )
        self.assertTrue(
            db.delete_generic_api_email("unison-libel-1a@icloud.com", source="mail.ai1998.xyz")
        )

    def test_configured_dynamic_source_is_visible_before_import(self):
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        with patch.object(email_config, "EMAIL_SOURCE", "mail.ai1998.xyz"):
            summary = client.get("/api/summary").get_json()
        self.assertEqual(summary["configured_sources"], ["mail.ai1998.xyz"])
        self.assertEqual(summary["per_source"]["mail.ai1998.xyz"]["total"], 0)
        self.assertEqual(
            [item["source"] for item in summary["generic_api_sources"]],
            ["mail.ai1998.xyz"],
        )

    def test_email_provider_resolves_dynamic_source(self):
        db.import_generic_api_emails([
            {"email": "unison-libel-1a@icloud.com", "code_url": PICKUP_URL},
        ])
        self.assertEqual(
            email_provider.resolve_email_source("unison-libel-1a@icloud.com"),
            "mail.ai1998.xyz",
        )
        self.assertEqual(
            email_provider.parse_email_sources("mail.ai1998.xyz,outlook"),
            ["mail.ai1998.xyz", "outlook"],
        )
        self.assertEqual(
            email_provider.parse_email_sources("MAIL.AI1998.XYZ,OUTLOOK"),
            ["mail.ai1998.xyz", "outlook"],
        )
        self.assertEqual(
            email_provider.parse_email_sources("MAIL.AI1998.XYZ.,OUTLOOK"),
            ["mail.ai1998.xyz", "outlook"],
        )

    @patch("webui.app.svc.submit_registration")
    def test_web_import_exposes_domain_as_pool_and_registration_source(self, submit):
        submit.return_value = []
        client = create_app(auth_code="test-auth").test_client()
        client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

        imported = client.post("/api/outlook/import", json={
            "source": "generic_api",
            "as_registered": False,
            "text": f"unison-libel-1a@icloud.com----{PICKUP_URL}",
        })
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.get_json()["sources"], ["mail.ai1998.xyz"])

        summary = client.get("/api/summary").get_json()
        self.assertEqual(summary["per_source"]["mail.ai1998.xyz"]["available"], 1)
        self.assertEqual(
            [item["source"] for item in summary["generic_api_sources"]],
            ["mail.ai1998.xyz"],
        )

        rows = client.get("/api/outlook?source=mail.ai1998.xyz").get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "mail.ai1998.xyz")

        with patch.object(email_config, "USE_EMAIL_SERVICE", True):
            response = client.post("/api/jobs", json={
                "count": 1,
                "workers": 1,
                "source": "mail.ai1998.xyz",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(submit.call_args.kwargs["email_source"], "mail.ai1998.xyz")

        changed = client.post("/api/outlook/status", json={
            "email": "unison-libel-1a@icloud.com",
            "source": "mail.ai1998.xyz",
            "status": "disabled",
        })
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(
            db.get_generic_api_email_by_email("unison-libel-1a@icloud.com")["status"],
            "disabled",
        )


if __name__ == "__main__":
    unittest.main()
