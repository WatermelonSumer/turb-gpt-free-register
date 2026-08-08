# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import email as email_config
from webui.app import create_app


class GPTMailWebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_gptmail_without_api_key_before_creating_tasks(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", ""):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("请填写 GPTMail API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_gptmail_key_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "gptmail"
        ), patch.object(email_config, "GPTMAIL_API_KEY", "key-123"):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        self.assertEqual(submit_registration.call_count, 1)
        self.assertEqual(submit_registration.call_args.kwargs["count"], 1)
        self.assertEqual(submit_registration.call_args.kwargs["workers"], 1)

    @patch("webui.app.db.domain_email_pool_summary", return_value={"total": 0, "available": 0, "used": 0, "failed": 0})
    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.db.count_accounts", return_value=0)
    def test_summary_does_not_count_gptmail_as_outlook_pool(self, count_accounts, outlook_pool_summary, domain_pool_summary):
        """
        gptmail 是按需生成的临时邮箱，不能把 outlook 池的数字当成它的容量。

        注意：/api/summary 现在会读所有本地池来拼 per_source（注册页下拉按来源取数用），
        所以这里断言的是「结果里不掺 outlook 的数」，而不是「没调用过 outlook 池」。
        """
        outlook_pool_summary.return_value = {"total": 7, "available": 5, "used": 2, "failed": 0}
        with patch.object(email_config, "EMAIL_SOURCE", "gptmail"):
            response = self.client.get("/api/summary")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        # 配置是 gptmail，outlook 的 7/5 不能漏进合计
        self.assertEqual(body["outlook_total"], 0)
        self.assertEqual(body["outlook_available"], 0)
        self.assertEqual(body["configured_sources"], ["gptmail"])
        # 临时邮箱标记为不限量，前端显示 ∞ 而不是 0
        self.assertTrue(body["per_source"]["gptmail"]["unlimited"])
        # outlook 的真实数字仍可单独查到（供注册页下拉切到 outlook 时显示）
        self.assertEqual(body["per_source"]["outlook"]["available"], 5)

    @patch("webui.app.svc.submit_registration")
    def test_jobs_rejects_mailnest_without_api_key_before_creating_tasks(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "mailnest"
        ), patch.object(email_config, "MAIL_NEST_API_KEY", "", create=True), patch.object(
            email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 400)
        self.assertIn("MailNest API Key", response.get_json()["error"])
        submit_registration.assert_not_called()

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_mailnest_key_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "mailnest"
        ), patch.object(email_config, "MAIL_NEST_API_KEY", "key-123", create=True), patch.object(
            email_config, "MAIL_NEST_PROJECT_CODE", "chatgpt001", create=True
        ):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        self.assertEqual(submit_registration.call_count, 1)
        self.assertEqual(submit_registration.call_args.kwargs["count"], 1)
        self.assertEqual(submit_registration.call_args.kwargs["workers"], 1)

    @patch("webui.app.svc.submit_registration")
    def test_jobs_allows_cloudmail_without_manual_domains(self, submit_registration):
        submit_registration.return_value = []
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "cloudmail"
        ), patch.object(email_config, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True), patch.object(
            email_config, "CLOUDMAIL_AUTH_TOKEN", "token", create=True
        ), patch.object(email_config, "CLOUDMAIL_DOMAINS", [], create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        self.assertEqual(submit_registration.call_count, 1)
        self.assertEqual(submit_registration.call_args.kwargs["count"], 1)
        self.assertEqual(submit_registration.call_args.kwargs["workers"], 1)

    @patch("webui.app.db.outlook_pool_summary")
    @patch("webui.app.svc.submit_registration", return_value=[{"id": 1}])
    def test_jobs_with_cloudmail_config_does_not_check_outlook_pool(self, submit_registration, outlook_pool_summary):
        outlook_pool_summary.return_value = {"total": 0, "available": 0, "used": 0, "failed": 0}
        with patch.object(email_config, "USE_EMAIL_SERVICE", True), patch.object(
            email_config, "EMAIL_SOURCE", "cloudmail"
        ), patch.object(email_config, "CLOUDMAIL_API_BASE", "https://mail.example.com", create=True), patch.object(
            email_config, "CLOUDMAIL_AUTH_TOKEN", "token", create=True
        ), patch.object(email_config, "CLOUDMAIL_DOMAINS", ["example.com"], create=True):
            response = self.client.post("/api/jobs", json={"count": 1, "workers": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["warning"], "")
        outlook_pool_summary.assert_not_called()
        self.assertEqual(submit_registration.call_count, 1)
        self.assertEqual(submit_registration.call_args.kwargs["count"], 1)
        self.assertEqual(submit_registration.call_args.kwargs["workers"], 1)
