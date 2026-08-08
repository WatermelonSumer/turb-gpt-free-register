# -*- coding: utf-8 -*-
"""孤独哥 CDK 的 WebUI 接口测试：导入卡密、兑换后回显到邮箱池。"""
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
            patch.object(db, "_LONELY_CARD_JSON", root / "cdk.json"),
            patch.object(db, "_LONELY_CARD_TXT", root / "cdk.txt"),
            patch.object(db, "_LONELY_EMAIL_JSON", root / "emails.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_import_cdk_lines(self):
        resp = self.client.post("/api/outlook/import", json={
            "source": "lonely",
            "text": "MAIL-TEST-QUOTA-0001\nMAIL-AAAA-BBBB-CCCC\n# 注释行\n\n",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["inserted"], 2)
        self.assertEqual(body["parsed"], 2)
        # CDK 不能按「已注册账号」导入
        self.assertFalse(body["as_registered"])

    def test_import_rejects_empty(self):
        resp = self.client.post("/api/outlook/import", json={"source": "lonely", "text": "\n# x\n"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("卡密", resp.get_json()["error"])

    def test_cards_endpoint_hides_secrets_and_lists_emails(self):
        db.import_lonely_cards([{"card_code": "MAIL-K1"}])
        db.update_lonely_card_activation(
            "MAIL-K1", api_key="pk_secret", api_secret="sk_secret",
            product_name="ChatGPT", total_quota=100, used_quota=1, remaining_quota=99,
        )
        db.record_lonely_redeemed_email(
            card_code="MAIL-K1", email="demo@gmail.com", session_id="2", remaining_quota=99,
        )

        resp = self.client.get("/api/lonely/cards")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        card = body["cards"][0]
        self.assertEqual(card["card_code"], "MAIL-K1")
        self.assertEqual(card["remaining_quota"], 99)
        self.assertEqual(card["emails"][0]["email"], "demo@gmail.com")
        # 密钥绝不能下发
        self.assertNotIn("api_secret", card)
        self.assertNotIn("api_key", card)
        self.assertIn("pk_secret"[:6], card["api_key_preview"])

    def test_redeemed_email_shows_in_pool_list(self):
        """兑换后邮箱池按 source=lonely 应该能查到 —— 这就是前端回显。"""
        db.import_lonely_cards([{"card_code": "MAIL-K2"}])
        db.record_lonely_redeemed_email(
            card_code="MAIL-K2", email="echo@gmail.com", session_id="7", remaining_quota=50,
        )
        resp = self.client.get("/api/outlook?source=lonely")
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "echo@gmail.com")
        self.assertEqual(rows[0]["source"], "lonely")

    def test_pool_list_all_includes_lonely(self):
        db.import_lonely_cards([{"card_code": "MAIL-K3"}])
        db.record_lonely_redeemed_email(
            card_code="MAIL-K3", email="inall@gmail.com", session_id="8", remaining_quota=3,
        )
        resp = self.client.get("/api/outlook?source=all")
        emails = [r["email"] for r in resp.get_json()]
        self.assertIn("inall@gmail.com", emails)

    def test_delete_redeemed_email(self):
        db.import_lonely_cards([{"card_code": "MAIL-K4"}])
        db.record_lonely_redeemed_email(
            card_code="MAIL-K4", email="gone@gmail.com", session_id="9", remaining_quota=1,
        )
        resp = self.client.post("/api/outlook/delete", json={"source": "lonely", "email": "gone@gmail.com"})
        self.assertTrue(resp.get_json()["deleted"])
        self.assertEqual(len(db.list_lonely_email_pool()), 0)
        # 卡本身还在
        self.assertIsNotNone(db.get_lonely_card("MAIL-K4"))


if __name__ == "__main__":
    unittest.main()
