# -*- coding: utf-8 -*-
"""
注册页手动选邮箱来源。

历史 bug：submit_registration(email_source=...) 只把来源写进任务记录，
_prepare_registration_args 里 acquire_email() 不传参、直接读 EMAIL_SOURCE，
导致导入了孤独哥 CDK 但配置还是 outlook 时注册跑不到新池子上。
"""
import unittest
from unittest.mock import patch

from core import email_provider
from core import registration_service as svc
from webui.app import create_app


class AcquireEmailOverrideTest(unittest.TestCase):
    def test_source_arg_overrides_config(self):
        with patch.object(email_provider, "_pick_from_source", lambda s: f"{s}@x.com"):
            self.assertEqual(email_provider.acquire_email("lonely_web"), "lonely_web@x.com")
            self.assertEqual(email_provider.acquire_email("lonely"), "lonely@x.com")

    def test_none_falls_back_to_config(self):
        from config import email as cfg
        with patch.object(email_provider, "_pick_from_source", lambda s: f"{s}@x.com"), \
             patch.object(cfg, "EMAIL_SOURCE", "generic_api"):
            self.assertEqual(email_provider.acquire_email(), "generic_api@x.com")

    def test_multi_source_override_falls_back_in_order(self):
        """选了多来源时按顺序兜底：前面的失败才用后面的。"""
        def picker(source):
            if source == "lonely_web":
                raise RuntimeError("没卡了")
            return f"{source}@x.com"

        with patch.object(email_provider, "_pick_from_source", picker):
            self.assertEqual(email_provider.acquire_email("lonely_web,outlook"), "outlook@x.com")


class PrepareArgsThreadsSourceTest(unittest.TestCase):
    """来源必须一路传到 acquire_email，不能半路丢掉。"""

    def test_prepare_args_passes_source_through(self):
        seen = {}

        def fake_acquire(source=None):
            seen["source"] = source
            return "picked@x.com"

        from config import email as ecfg, register as rcfg
        with patch("core.email_provider.acquire_email", fake_acquire), \
             patch.object(ecfg, "USE_EMAIL_SERVICE", True), \
             patch.object(rcfg, "REGISTER_EMAIL", ""):
            email, name, birthday = svc._prepare_registration_args("lonely_web")
        self.assertEqual(email, "picked@x.com")
        self.assertEqual(seen["source"], "lonely_web")

    def test_prepare_args_none_source_reaches_acquire_as_none(self):
        seen = {}

        def fake_acquire(source=None):
            seen["source"] = source
            return "picked@x.com"

        from config import email as ecfg, register as rcfg
        with patch("core.email_provider.acquire_email", fake_acquire), \
             patch.object(ecfg, "USE_EMAIL_SERVICE", True), \
             patch.object(rcfg, "REGISTER_EMAIL", ""):
            svc._prepare_registration_args(None)
        self.assertIsNone(seen["source"])


class JobsApiSourceTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_source_forwarded_to_service(self, submit):
        submit.return_value = []
        from config import email as cfg
        with patch.object(cfg, "USE_EMAIL_SERVICE", True), patch.object(cfg, "EMAIL_SOURCE", "outlook"):
            resp = self.client.post("/api/jobs", json={"count": 1, "workers": 1, "source": "lonely_web"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(submit.call_args.kwargs["email_source"], "lonely_web")
        self.assertEqual(resp.get_json()["source"], "lonely_web")

    @patch("webui.app.svc.submit_registration")
    def test_no_source_uses_config(self, submit):
        submit.return_value = []
        from config import email as cfg
        with patch.object(cfg, "USE_EMAIL_SERVICE", True), patch.object(cfg, "EMAIL_SOURCE", "generic_api"):
            resp = self.client.post("/api/jobs", json={"count": 1, "workers": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(submit.call_args.kwargs["email_source"], "generic_api")

    @patch("webui.app.svc.submit_registration")
    def test_bad_source_rejected_not_silently_outlook(self, submit):
        """
        parse_email_sources 兜底会返回 ["outlook"]，所以传错来源必须显式报错，
        否则会静默跑到 outlook 池上 —— 正是要修的那种「自动选择」。
        """
        submit.return_value = []
        from config import email as cfg
        with patch.object(cfg, "USE_EMAIL_SERVICE", True):
            resp = self.client.post("/api/jobs", json={"count": 1, "workers": 1, "source": "nonexistent"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("未知邮箱来源", resp.get_json()["error"])
        submit.assert_not_called()

    @patch("webui.app.svc.submit_registration")
    def test_capacity_warning_follows_selected_source(self, submit):
        """选 lonely_web 时容量提示要说孤独哥网页版，不能报 outlook 的数。"""
        submit.return_value = []
        from config import email as cfg
        from core import db
        with patch.object(cfg, "USE_EMAIL_SERVICE", True), \
             patch.object(cfg, "EMAIL_SOURCE", "outlook"), \
             patch.object(db, "lonely_web_email_pool_summary", lambda: {"available": 0, "total": 0, "used": 0, "failed": 0}):
            resp = self.client.post("/api/jobs", json={"count": 5, "workers": 1, "source": "lonely_web"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("孤独哥网页版", resp.get_json()["warning"])


class SummaryPerSourceTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_summary_reports_all_pools_not_only_configured(self):
        """
        导入了 CDK 但配置还是 outlook 时，per_source 里也要能看到 CDK 的数量，
        否则前端「可用」永远是 0，看着像没导进去。
        """
        from config import email as cfg
        from core import db
        with patch.object(cfg, "EMAIL_SOURCE", "outlook"), \
             patch.object(db, "lonely_web_email_pool_summary",
                          lambda: {"available": 7, "total": 9, "used": 2, "failed": 0}):
            s = self.client.get("/api/summary").get_json()
        self.assertEqual(s["configured_sources"], ["outlook"])
        self.assertEqual(s["per_source"]["lonely_web"]["available"], 7)

    def test_temp_mail_sources_marked_unlimited(self):
        s = self.client.get("/api/summary").get_json()
        for src in ("gptmail", "mailnest", "cloudmail", "cloudflare"):
            self.assertTrue(s["per_source"][src]["unlimited"], src)
        self.assertNotIn("unlimited", s["per_source"]["outlook"])


if __name__ == "__main__":
    unittest.main()
