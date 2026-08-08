# -*- coding: utf-8 -*-
"""孤独哥 CDK 邮箱来源测试：签名算法与重试策略。"""
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

from core import lonely_mail_client as lonely


def _node_sign(method, path, body, secret, timestamp):
    """按文档 Node.js 示例独立复算签名，用于交叉验证。"""
    body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body else ""
    body_hash = hashlib.sha256(body_text.encode()).hexdigest()
    sign_text = "\n".join([method.upper(), path, timestamp, body_hash])
    return hmac.new(secret.encode(), sign_text.encode(), hashlib.sha256).hexdigest()


class SignatureTest(unittest.TestCase):
    def test_empty_body_hash_matches_doc(self):
        self.assertEqual(hashlib.sha256(b"").hexdigest(), lonely._EMPTY_BODY_SHA256)

    def test_get_signature_matches_doc_algorithm(self):
        path = "/api/v1/external/email/sessions/2"
        ts, sig = lonely._sign("GET", path, "", "sk_test")
        self.assertEqual(sig, _node_sign("GET", path, None, "sk_test", ts))

    def test_post_signature_matches_doc_algorithm(self):
        path = "/api/v1/external/email/sessions"
        body = {"externalSessionNo": "order-001"}
        body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        ts, sig = lonely._sign("POST", path, body_text, "sk_test")
        self.assertEqual(sig, _node_sign("POST", path, body, "sk_test", ts))


class RetryPolicyTest(unittest.TestCase):
    """兑换按 5 次 / 间隔 1s 重试，但永久性业务错误不该白等。"""

    def test_permanent_error_does_not_retry(self):
        calls = []

        def fake_request(*args, **kwargs):
            calls.append(1)
            raise lonely.LonelyMailError("该卡密不是邮箱API额度卡", code="2005")

        with patch.object(lonely, "_request", fake_request):
            with self.assertRaises(lonely.LonelyMailError) as ctx:
                lonely.activate_card("MAIL-XXXX")
        self.assertEqual(ctx.exception.code, "2005")
        self.assertEqual(len(calls), 1)

    def test_transient_error_retries_then_succeeds(self):
        calls = []

        def fake_request(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise lonely.LonelyMailError("connection reset")
            return {"apiKey": "pk_x", "apiSecret": "sk_x", "remainingQuota": 9}

        with patch.object(lonely, "_request", fake_request), \
             patch.object(lonely.time, "sleep", lambda *_: None):
            info = lonely.activate_card("MAIL-XXXX")
        self.assertEqual(info["apiKey"], "pk_x")
        self.assertEqual(len(calls), 3)

    def test_retry_gives_up_after_five_attempts(self):
        calls = []

        def fake_request(*args, **kwargs):
            calls.append(1)
            raise lonely.LonelyMailError("timeout")

        with patch.object(lonely, "_request", fake_request), \
             patch.object(lonely.time, "sleep", lambda *_: None):
            with self.assertRaises(lonely.LonelyMailError):
                lonely.activate_card("MAIL-XXXX")
        self.assertEqual(len(calls), 5)
        self.assertEqual(lonely.ACTIVATE_RETRY_TIMES, 5)


class RedeemEchoTest(unittest.TestCase):
    """
    核心场景：邮箱池按 CDK 导入，注册时兑换出邮箱，
    兑换结果必须能被邮箱池列表查到（前端回显依赖这个）。
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from core import db

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._patches = [
            patch.object(db, "_LONELY_CARD_JSON", root / "cdk.json"),
            patch.object(db, "_LONELY_CARD_TXT", root / "cdk.txt"),
            patch.object(db, "_LONELY_EMAIL_JSON", root / "emails.json"),
        ]
        for p in self._patches:
            p.start()
        self.db = db

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_import_then_redeem_shows_in_pool(self):
        db = self.db
        inserted, skipped = db.import_lonely_cards([{"card_code": "MAIL-TEST-QUOTA-0001"}])
        self.assertEqual((inserted, skipped), (1, 0))

        # 重复导入同一张卡应被跳过
        self.assertEqual(db.import_lonely_cards([{"card_code": "MAIL-TEST-QUOTA-0001"}]), (0, 1))

        # 未激活的卡也应可领取（额度未知按可用处理）
        card = db.claim_next_lonely_card()
        self.assertIsNotNone(card)
        self.assertEqual(card["card_code"], "MAIL-TEST-QUOTA-0001")

        db.update_lonely_card_activation(
            "MAIL-TEST-QUOTA-0001",
            api_key="pk_x", api_secret="sk_x",
            product_name="ChatGPT", total_quota=100, used_quota=0, remaining_quota=100,
        )

        # 兑换第一个邮箱
        db.record_lonely_redeemed_email(
            card_code="MAIL-TEST-QUOTA-0001",
            email="demo1@gmail.com",
            session_id="2",
            remaining_quota=99,
        )
        pool = db.list_lonely_email_pool()
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["email"], "demo1@gmail.com")
        self.assertEqual(pool[0]["session_id"], "2")
        # 列表字段形状要和其它来源一致，前端才不用写特例
        self.assertIn("copy_line", pool[0])
        self.assertIn("code_url", pool[0])

        # 一张卡兑换第二个邮箱：卡不该被独占
        card2 = db.claim_next_lonely_card()
        self.assertIsNotNone(card2)
        db.record_lonely_redeemed_email(
            card_code="MAIL-TEST-QUOTA-0001",
            email="demo2@gmail.com",
            session_id="3",
            remaining_quota=98,
        )
        self.assertEqual(len(db.list_lonely_email_pool()), 2)

        # 卡列表应带出这两个邮箱，并累计兑换次数
        cards = db.list_lonely_card_pool()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["redeemed_count"], 2)
        self.assertEqual(cards[0]["remaining_quota"], 98)
        self.assertEqual({e["email"] for e in cards[0]["emails"]},
                         {"demo1@gmail.com", "demo2@gmail.com"})
        # 密钥不能下发到前端
        self.assertNotIn("api_secret", cards[0])
        self.assertNotIn("api_key", cards[0])

        # 汇总里 available 是「还能兑换多少个」= 剩余额度
        summary = db.lonely_email_pool_summary()
        self.assertEqual(summary["available"], 98)
        self.assertEqual(summary["used"], 2)

    def test_quota_exhausted_card_not_claimable(self):
        db = self.db
        db.import_lonely_cards([{"card_code": "MAIL-DONE"}])
        db.update_lonely_card_activation("MAIL-DONE", api_key="pk", api_secret="sk", remaining_quota=1)
        db.record_lonely_redeemed_email(
            card_code="MAIL-DONE", email="last@gmail.com", session_id="9", remaining_quota=0,
        )
        # 额度归零后卡应自动 exhausted，不再被领取
        self.assertIsNone(db.claim_next_lonely_card())
        self.assertEqual(db.get_lonely_card("MAIL-DONE")["status"], "exhausted")

    def test_released_email_is_not_recycled_as_available(self):
        """额度已扣，邮箱不能退回服务端；release 不该把它变回 available。"""
        db = self.db
        db.import_lonely_cards([{"card_code": "MAIL-R"}])
        db.record_lonely_redeemed_email(
            card_code="MAIL-R", email="fail@gmail.com", session_id="7", remaining_quota=5,
        )
        db.release_lonely_redeemed_email("fail@gmail.com", status="available", note="注册失败")
        row = db.get_lonely_redeemed_email("fail@gmail.com")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["note"], "注册失败")


if __name__ == "__main__":
    unittest.main()
