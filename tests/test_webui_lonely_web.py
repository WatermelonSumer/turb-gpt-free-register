# -*- coding: utf-8 -*-
"""孤独哥网页版 WebUI 接口测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class LonelyWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._patches = [
            patch.object(db, "_LONELY_WEB_CARD_JSON", root / "cdk.json"),
            patch.object(db, "_LONELY_WEB_CARD_TXT", root / "cdk.txt"),
            patch.object(db, "_LONELY_WEB_EMAIL_JSON", root / "emails.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_import_web_cdk(self):
        resp = self.client.post("/api/outlook/import", json={
            "source": "lonely_web",
            "text": "MAIL-TEST-WEB-0001\nMAIL-TEST-WEB-0002\n",
        })
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["inserted"], 2)
        self.assertEqual(body["source"], "lonely_web")
        self.assertFalse(body["as_registered"])
        # 不该跑到额度卡版的池子里
        self.assertEqual(db.lonely_web_card_pool_summary()["available"], 2)

    def test_cards_endpoint(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-W1"}])
        db.record_lonely_web_redeemed_email(
            card_code="MAIL-W1", email="w1@gmail.com", session_id="1352", product_name="ChatGPT",
        )
        resp = self.client.get("/api/lonely-web/cards")
        card = resp.get_json()["cards"][0]
        self.assertEqual(card["card_code"], "MAIL-W1")
        self.assertEqual(card["email"], "w1@gmail.com")
        self.assertEqual(card["status"], "used")

    def test_redeemed_email_echoes_in_pool(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-W2"}])
        db.record_lonely_web_redeemed_email(
            card_code="MAIL-W2", email="echo@gmail.com", session_id="9",
        )
        rows = self.client.get("/api/outlook?source=lonely_web").get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "echo@gmail.com")
        self.assertEqual(rows[0]["source"], "lonely_web")

    def test_lookup_endpoint_reports_not_redeemed(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-W3"}])
        from core import lonely_web_client as web
        with patch.object(web, "_request", lambda *a, **k: ("2005", {"_msg": "卡密尚未兑换"})):
            resp = self.client.post("/api/lonely-web/cards/lookup", json={"card_code": "MAIL-W3"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("尚未兑换", resp.get_json()["error"])

    def test_lookup_backfills_email_redeemed_outside(self):
        """在网页上手动兑换过的卡，查一次就应补上本地记录并回显。"""
        db.import_lonely_web_cards([{"card_code": "MAIL-W4"}])
        from core import lonely_web_client as web
        payload = ("0", {
            "type": "email",
            "email": {
                "session": {"sessionId": 1352, "emailAddress": "backfill@gmail.com",
                            "productName": "ChatGPT", "status": "active",
                            "expiresAt": "2026-08-09T03:30:22"},
                "codes": [{"id": 1, "code": "785901", "receivedAt": "2026-08-09T00:16:10"}],
            },
        })
        with patch.object(web, "_request", lambda *a, **k: payload):
            resp = self.client.post("/api/lonely-web/cards/lookup", json={"card_code": "MAIL-W4"})
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["email"], "backfill@gmail.com")
        self.assertEqual(body["codes"][0]["code"], "785901")
        self.assertIsNotNone(db.get_lonely_web_redeemed_email("backfill@gmail.com"))

    def test_delete_web_card(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-W5"}])
        resp = self.client.post("/api/lonely-web/cards/delete", json={"card_code": "MAIL-W5"})
        self.assertTrue(resp.get_json()["ok"])
        self.assertIsNone(db.get_lonely_web_card("MAIL-W5"))

    def test_pool_all_includes_web(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-W6"}])
        db.record_lonely_web_redeemed_email(
            card_code="MAIL-W6", email="inall-web@gmail.com", session_id="3",
        )
        emails = [r["email"] for r in self.client.get("/api/outlook?source=all").get_json()]
        self.assertIn("inall-web@gmail.com", emails)


if __name__ == "__main__":
    unittest.main()
