# -*- coding: utf-8 -*-
"""孤独哥 CDK 网页版测试。

响应样例取自实测（MAIL-TEST-WEB-0001 / MAIL-TEST-WEB-0002）：
    redeem 首次   -> {"code":0,"data":{"type":"email"}}          # 不返回邮箱
    redeem 已用过 -> {"code":2004,"msg":"卡密使用中"}              # 不是失败
    lookup 未兑换 -> {"code":2005,"msg":"卡密尚未兑换"}
    lookup 已兑换 -> data.email.session.emailAddress + codes[]
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from core import lonely_web_client as web


def _lookup_ok(email="demo@gmail.com", session_id=1352, codes=None):
    return "0", {
        "type": "email",
        "email": {
            "session": {
                "sessionId": session_id,
                "emailAddress": email,
                "productName": "ChatGPT",
                "status": "active",
                "expiresAt": "2026-08-09T03:30:22",
                "isMicrosoftOauth": False,
                "emailSourceType": "api_url",
            },
            "codes": codes if codes is not None else [],
        },
    }


class RedeemTest(unittest.TestCase):
    def test_first_redeem_returns_redeemed(self):
        with patch.object(web, "_request", lambda *a, **k: ("0", {"type": "email"})):
            self.assertEqual(web.redeem_card("MAIL-NEW"), "redeemed")

    def test_already_used_card_is_in_use_not_error(self):
        """2004「卡密使用中」是正常分支 —— 卡已绑定，接下来直接 lookup。"""
        with patch.object(web, "_request", lambda *a, **k: ("2004", {"_msg": "卡密使用中"})):
            self.assertEqual(web.redeem_card("MAIL-USED"), "in_use")

    def test_permanent_error_does_not_retry(self):
        calls = []

        def fake(*a, **k):
            calls.append(1)
            return "2001", {"_msg": "卡密不存在"}

        with patch.object(web, "_request", fake), patch.object(web.time, "sleep", lambda *_: None):
            with self.assertRaises(web.LonelyWebError) as ctx:
                web.redeem_card("MAIL-BAD")
        self.assertEqual(ctx.exception.code, "2001")
        self.assertEqual(len(calls), 1)

    def test_transient_error_retries_three_times(self):
        calls = []

        def fake(*a, **k):
            calls.append(1)
            raise web.LonelyWebError("connection reset")

        with patch.object(web, "_request", fake), patch.object(web.time, "sleep", lambda *_: None):
            with self.assertRaises(web.LonelyWebError):
                web.redeem_card("MAIL-X")
        self.assertEqual(web.REDEEM_RETRY_TIMES, 3)
        self.assertEqual(len(calls), 3)


class LookupTest(unittest.TestCase):
    def test_lookup_parses_session_and_codes(self):
        codes = [
            {"id": 7769, "code": "785901", "receivedAt": "2026-08-09T00:16:10"},
            {"id": 7768, "code": "621135", "receivedAt": "2026-08-09T00:15:21"},
        ]
        with patch.object(web, "_request", lambda *a, **k: _lookup_ok(codes=codes)):
            info = web.lookup_order("MAIL-X", poll=False)
        self.assertEqual(info["email"], "demo@gmail.com")
        self.assertEqual(info["session_id"], "1352")
        self.assertEqual(info["status"], "active")
        self.assertEqual([c["code"] for c in info["codes"]], ["785901", "621135"])

    def test_lookup_before_redeem_raises_2005(self):
        with patch.object(web, "_request", lambda *a, **k: ("2005", {"_msg": "卡密尚未兑换"})):
            with self.assertRaises(web.LonelyWebError) as ctx:
                web.lookup_order("MAIL-NEW", poll=False)
        self.assertEqual(ctx.exception.code, "2005")

    def test_poll_flag_passed_through(self):
        seen = {}

        def fake(method, path, *, params=None, body=None, base_url=None, timeout=None):
            seen.update(params or {})
            return _lookup_ok()

        with patch.object(web, "_request", fake):
            web.lookup_order("MAIL-X", poll=True)
            self.assertEqual(seen["poll"], "true")
            web.lookup_order("MAIL-X", poll=False)
            self.assertEqual(seen["poll"], "false")


class WebFlowTest(unittest.TestCase):
    """CDK 导入 → 兑换 → 取码 → 前端回显。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._patches = [
            patch.object(db, "_LONELY_WEB_CARD_JSON", root / "cdk.json"),
            patch.object(db, "_LONELY_WEB_CARD_TXT", root / "cdk.txt"),
            patch.object(db, "_LONELY_WEB_EMAIL_JSON", root / "emails.json"),
        ]
        for p in self._patches:
            p.start()
        web._CONTEXT_CACHE.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()
        web._CONTEXT_CACHE.clear()

    def _fake(self, codes=None, email="demo@gmail.com"):
        def fake(method, path, *, params=None, body=None, base_url=None, timeout=None):
            if path.endswith("/redeem"):
                return "0", {"type": "email"}
            if path.endswith("/order/lookup"):
                return _lookup_ok(email=email, codes=codes)
            raise AssertionError(f"未预期调用 {method} {path}")
        return fake

    def test_pick_account_redeems_and_records(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-TEST-WEB-0002"}])
        with patch.object(web, "_request", self._fake()):
            acc = web.pick_account()
        self.assertEqual(acc.email, "demo@gmail.com")
        self.assertEqual(acc.session_id, "1352")

        # 卡被独占（一卡一邮箱），邮箱回写到卡上
        card = db.get_lonely_web_card("MAIL-TEST-WEB-0002")
        self.assertEqual(card["status"], "used")
        self.assertEqual(card["email"], "demo@gmail.com")
        # 邮箱池能查到 —— 前端回显
        pool = db.list_lonely_web_email_pool()
        self.assertEqual([r["email"] for r in pool], ["demo@gmail.com"])
        self.assertIn("copy_line", pool[0])

    def test_one_card_only_yields_one_email(self):
        """卡被领走就 used，第二次领不到 —— 和额度卡版不同。"""
        db.import_lonely_web_cards([{"card_code": "MAIL-ONE"}])
        with patch.object(web, "_request", self._fake()):
            web.pick_account()
            web._CONTEXT_CACHE.clear()
            self.assertIsNone(db.claim_next_lonely_web_card())
            with self.assertRaises(web.LonelyWebError):
                web.pick_account()

    def test_fetch_otp_returns_newest(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-OTP"}])
        codes = [
            {"id": 2, "code": "785901", "receivedAt": "2026-08-09T00:16:10"},
            {"id": 1, "code": "621135", "receivedAt": "2026-08-09T00:15:21"},
        ]
        with patch.object(web, "_request", self._fake(codes=codes)):
            web.pick_account()
            otp = web.fetch_latest_otp("demo@gmail.com", settle_seconds=0, max_wait=5)
        self.assertEqual(otp, "785901")

    def test_old_codes_filtered_by_after_ts(self):
        """实测那张已用卡里留着 4 条旧码，必须按 after_ts 滤掉。"""
        from datetime import datetime
        db.import_lonely_web_cards([{"card_code": "MAIL-TS"}])
        codes = [{"id": 1, "code": "111111", "receivedAt": "2020-01-01T00:00:00"}]
        with patch.object(web, "_request", self._fake(codes=codes)):
            web.pick_account()
            with self.assertRaises(web.LonelyWebError):
                web.fetch_latest_otp(
                    "demo@gmail.com",
                    after_ts=datetime.now().timestamp(),
                    settle_seconds=0,
                    max_wait=1,
                )

    def test_redeem_failure_returns_card_to_pool(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-FAIL"}])

        def fake(method, path, *, params=None, body=None, base_url=None, timeout=None):
            return "2002", {"_msg": "额度已用完"}

        with patch.object(web, "_request", fake), patch.object(web.time, "sleep", lambda *_: None):
            with self.assertRaises(web.LonelyWebError):
                web.pick_account()
        card = db.get_lonely_web_card("MAIL-FAIL")
        self.assertEqual(card["status"], "available")
        self.assertIn("兑换失败", card["note"])
        self.assertEqual(len(db.list_lonely_web_email_pool()), 0)

    def test_redeem_success_waits_for_eventual_consistency_before_registration(self):
        """redeem 成功后短暂返回 2005/空邮箱时，仍应等到邮箱可用再交给注册流程。"""
        db.import_lonely_web_cards([{"card_code": "MAIL-EVENTUAL"}])
        lookup_calls = []

        def fake(method, path, *, params=None, body=None, base_url=None, timeout=None):
            if path.endswith("/redeem"):
                return "0", {"type": "email"}
            if path.endswith("/order/lookup"):
                lookup_calls.append(1)
                if len(lookup_calls) < 3:
                    return "2005", {"_msg": "卡密尚未兑换"}
                return _lookup_ok(email="eventual@gmail.com")
            raise AssertionError(f"未预期调用 {method} {path}")

        with patch.object(web, "_request", fake), patch.object(web.time, "sleep", lambda *_: None):
            account = web.pick_account()

        self.assertEqual(account.email, "eventual@gmail.com")
        self.assertEqual(len(lookup_calls), 3)
        self.assertEqual(db.get_lonely_web_card("MAIL-EVENTUAL")["status"], "used")

    def test_lookup_failure_after_redeem_keeps_card_used_until_registration_result(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-PENDING"}])

        def fake(method, path, *, params=None, body=None, base_url=None, timeout=None):
            if path.endswith("/redeem"):
                return "0", {"type": "email"}
            if path.endswith("/order/lookup"):
                return "2005", {"_msg": "卡密尚未兑换"}
            raise AssertionError(f"未预期调用 {method} {path}")

        with patch.object(web, "_request", fake), patch.object(web.time, "sleep", lambda *_: None):
            with self.assertRaises(web.LonelyWebError):
                web.pick_account()
        card = db.get_lonely_web_card("MAIL-PENDING")
        self.assertEqual(card["status"], "used")
        self.assertIn("兑换成功", card["note"])

    def test_registration_failure_marks_email_and_card_failed(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-REG-FAIL"}])
        with patch.object(web, "_request", self._fake()):
            web.pick_account()

        db.release_lonely_web_redeemed_email(
            "demo@gmail.com", status="failed", note="注册接口报错"
        )
        email_row = db.get_lonely_web_redeemed_email("demo@gmail.com")
        card = db.get_lonely_web_card("MAIL-REG-FAIL")
        self.assertEqual(email_row["status"], "failed")
        self.assertEqual(card["status"], "failed")
        self.assertEqual(card["note"], "注册接口报错")

    def test_summary_available_is_unredeemed_card_count(self):
        db.import_lonely_web_cards([{"card_code": "A"}, {"card_code": "B"}, {"card_code": "C"}])
        with patch.object(web, "_request", self._fake()):
            web.pick_account()
        s = db.lonely_web_email_pool_summary()
        self.assertEqual(s["available"], 2)
        self.assertEqual(s["used"], 1)

    def test_released_email_not_recycled(self):
        db.import_lonely_web_cards([{"card_code": "MAIL-R"}])
        with patch.object(web, "_request", self._fake()):
            web.pick_account()
        db.release_lonely_web_redeemed_email("demo@gmail.com", status="available", note="注册失败")
        row = db.get_lonely_web_redeemed_email("demo@gmail.com")
        self.assertEqual(row["status"], "failed")


if __name__ == "__main__":
    unittest.main()
