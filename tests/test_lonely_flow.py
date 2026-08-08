# -*- coding: utf-8 -*-
"""孤独哥全链路测试：CDK 导入 → 注册时兑换邮箱 → 取码 → 前端回显。

真实卡密无法在 CI 里用，所以打掉 _request，用文档给的响应样例驱动。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core import lonely_mail_client as lonely


class LonelyFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._patches = [
            patch.object(db, "_LONELY_CARD_JSON", root / "cdk.json"),
            patch.object(db, "_LONELY_CARD_TXT", root / "cdk.txt"),
            patch.object(db, "_LONELY_EMAIL_JSON", root / "emails.json"),
        ]
        for p in self._patches:
            p.start()
        lonely._CONTEXT_CACHE.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()
        lonely._CONTEXT_CACHE.clear()

    def _fake_api(self, codes=None):
        """按文档样例返回：激活 / 创建会话 / 查验证码。"""
        calls = []

        def fake_request(method, path, *, api_key="", api_secret="", body=None, base_url=None):
            calls.append((method, path, api_key, api_secret))
            if path.endswith("/cards/activate"):
                return {
                    "apiKey": "pk_xxx", "apiSecret": "sk_xxx", "productName": "ChatGPT",
                    "totalQuota": 100, "usedQuota": 0, "remainingQuota": 100,
                    "expiresAt": "2026-08-02T13:22:46",
                }
            if path.endswith("/email/sessions") and method == "POST":
                # 签名必须已带上密钥
                assert api_key and api_secret, "创建会话必须带签名密钥"
                return {
                    "sessionId": 2, "emailAddress": "demo@gmail.com", "productName": "ChatGPT",
                    "status": "active", "expiresAt": "2026-07-03T17:34:05",
                    "remainingQuota": 99, "codes": [],
                }
            if "/email/sessions/" in path and method == "GET":
                return {"sessionId": 2, "emailAddress": "demo@gmail.com", "status": "active",
                        "codes": codes if codes is not None else []}
            raise AssertionError(f"未预期的调用: {method} {path}")

        return fake_request, calls

    def test_full_redeem_and_fetch_otp(self):
        db.import_lonely_cards([{"card_code": "MAIL-TEST-QUOTA-0001"}])
        fake, calls = self._fake_api(codes=[
            {"id": 1, "code": "123456", "receivedAt": "2026-07-03T13:40:00"},
        ])

        with patch.object(lonely, "_request", fake):
            account = lonely.pick_account()
            self.assertEqual(account.email, "demo@gmail.com")
            self.assertEqual(account.session_id, "2")
            self.assertEqual(account.card_code, "MAIL-TEST-QUOTA-0001")

            # 兑换结果已落库 —— 前端邮箱池能查到
            pool = db.list_lonely_email_pool()
            self.assertEqual([r["email"] for r in pool], ["demo@gmail.com"])
            # 密钥缓存下来了，卡额度也同步了
            card = db.get_lonely_card("MAIL-TEST-QUOTA-0001")
            self.assertEqual(card["api_key"], "pk_xxx")
            self.assertEqual(card["remaining_quota"], 99)
            self.assertEqual(card["redeemed_count"], 1)

            # 取码：settle=0 时立刻返回
            otp = lonely.fetch_latest_otp("demo@gmail.com", settle_seconds=0, max_wait=5)
            self.assertEqual(otp, "123456")

        activate_calls = [c for c in calls if c[1].endswith("/cards/activate")]
        self.assertEqual(len(activate_calls), 1, "同一张卡不该重复激活")

    def test_second_redeem_reuses_cached_keys(self):
        """第二次兑换不再调激活接口 —— 密钥已缓存，省一次往返。"""
        db.import_lonely_cards([{"card_code": "MAIL-K"}])
        fake, calls = self._fake_api()
        with patch.object(lonely, "_request", fake):
            lonely.pick_account()
            lonely._CONTEXT_CACHE.clear()
            lonely.pick_account()
        activate_calls = [c for c in calls if c[1].endswith("/cards/activate")]
        self.assertEqual(len(activate_calls), 1)

    def test_old_code_filtered_by_after_ts(self):
        """after_ts 之前的旧验证码要跳过，否则会拿到上一轮的码。"""
        import time
        from datetime import datetime

        db.import_lonely_cards([{"card_code": "MAIL-TS"}])
        old_iso = "2020-01-01T00:00:00"
        fake, _ = self._fake_api(codes=[{"id": 1, "code": "111111", "receivedAt": old_iso}])
        with patch.object(lonely, "_request", fake):
            lonely.pick_account()
            with self.assertRaises(lonely.LonelyMailError):
                lonely.fetch_latest_otp(
                    "demo@gmail.com",
                    after_ts=datetime.now().timestamp(),
                    settle_seconds=0,
                    max_wait=1,
                )

    def test_session_create_failure_returns_card_to_pool(self):
        """创建会话失败不扣额度，卡要放回池子并记下原因。"""
        db.import_lonely_cards([{"card_code": "MAIL-FAIL"}])

        def fake_request(method, path, *, api_key="", api_secret="", body=None, base_url=None):
            if path.endswith("/cards/activate"):
                return {"apiKey": "pk", "apiSecret": "sk", "remainingQuota": 10}
            raise lonely.LonelyMailError("API邮箱库存不足", code="1002")

        with patch.object(lonely, "_request", fake_request), \
             patch.object(lonely.time, "sleep", lambda *_: None):
            with self.assertRaises(lonely.LonelyMailError):
                lonely.pick_account()

        card = db.get_lonely_card("MAIL-FAIL")
        self.assertEqual(card["status"], "available")
        self.assertIn("兑换失败", card["note"])
        # 没兑换成功就不该有邮箱记录
        self.assertEqual(len(db.list_lonely_email_pool()), 0)


if __name__ == "__main__":
    unittest.main()
