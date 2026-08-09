# -*- coding: utf-8 -*-
import json
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from config import browser_use, env_loader
from core import browser_use_registration as registration
from webui import config_editor


class _Response:
    def __init__(self, status=200, payload=None, body=""):
        self.status = status
        self._payload = payload
        self._body = body or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def text(self):
        return self._body


class _Request:
    def __init__(self, activate_payload=None):
        self.calls = []
        self.activate_payload = activate_payload or {"success": True}

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if url.endswith("/api/auth/csrf"):
            return _Response(payload={"csrfToken": "csrf-token"})
        if url == "https://auth.example.test/authorize":
            return _Response(payload={})
        if url == "https://auth.example.test/continue":
            return _Response(payload={})
        if url.endswith("/api/auth/session"):
            return _Response(payload={"accessToken": "fresh-access-token"})
        return _Response(payload={})

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        if "/api/auth/signin/openai?" in url:
            return _Response(payload={"url": "https://auth.example.test/authorize"})
        if url.endswith("/email-otp/validate"):
            return _Response(payload={"continue_url": "https://auth.example.test/continue"})
        if url.endswith("/accounts/mfa/enroll"):
            return _Response(payload={"secret": "JBSWY3DPEHPK3PXP", "session_id": "enroll-1"})
        if url.endswith("/activate_enrollment"):
            return _Response(payload=self.activate_payload)
        return _Response(payload={})


class _Context:
    def __init__(self, request):
        self.request = request

    def cookies(self):
        return [{"name": "oai-did", "value": "device-1"}]


class BrowserUseForceTests(unittest.TestCase):
    def test_flag_and_webui_field(self):
        # The imported config can be overridden by the developer's local .env.
        self.assertIsInstance(browser_use.BROWSER_USE_FORCE_PASSWORD_2FA, bool)
        source = Path(browser_use.__file__).read_text(encoding="utf-8")
        self.assertIn("BROWSER_USE_FORCE_PASSWORD_2FA: bool = False", source)
        fields = {item["key"]: item for item in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["BROWSER_USE_FORCE_PASSWORD_2FA"]["type"], "bool")
        self.assertEqual(fields["BROWSER_USE_FORCE_PASSWORD_2FA"]["group"], "Browser Use")

    def test_use_password_locator_uses_stable_href(self):
        page = Mock()
        with patch.object(registration, "_click_first", return_value=True) as click:
            self.assertTrue(registration._click_use_password_if_present(page))
        selectors = click.call_args.args[1]
        self.assertEqual(
            selectors[0],
            "a[data-login-web-auth-control='true'][href*='/create-account/password']",
        )

    def test_flag_env_override_parses_true(self):
        old_loaded = env_loader._LOADED
        env_loader._LOADED = True
        namespace = {"BROWSER_USE_FORCE_PASSWORD_2FA": False}
        try:
            with patch.dict(os.environ, {"BROWSER_USE_FORCE_PASSWORD_2FA": "true"}, clear=True):
                env_loader.apply_env_overrides(namespace, {"BROWSER_USE_FORCE_PASSWORD_2FA": "bool"})
        finally:
            env_loader._LOADED = old_loaded
        self.assertTrue(namespace["BROWSER_USE_FORCE_PASSWORD_2FA"])

    def test_force_password_skips_passwordless_and_requires_transition(self):
        page = Mock()
        states = iter([
            {"state": "password", "url": "https://auth.openai.com/create-account/password"},
            {"state": "email_verification", "url": "https://auth.openai.com/email-verification"},
        ])
        with patch.object(registration, "_browser_use_heartbeat", return_value=page), \
             patch.object(registration, "_quick_auth_state", side_effect=lambda _: next(states)), \
             patch.object(registration, "_fill_first", return_value=True), \
             patch.object(registration, "_click_first", return_value=True), \
             patch.object(registration, "_click_passwordless_signup_if_present") as passwordless, \
             patch.object(registration, "_registration_password", return_value="Password!1234"), \
             patch.object(registration, "_bu_delay"):
            result = registration._fill_password_if_present(
                page,
                "user@example.test",
                timeout=1,
                force=True,
            )
        self.assertEqual(result, "Password!1234")
        passwordless.assert_not_called()

    def test_force_password_enters_password_route_from_email_verification(self):
        page = Mock()
        states = iter([
            {"state": "email_verification", "url": "https://auth.openai.com/email-verification"},
            {"state": "password", "url": "https://auth.openai.com/create-account/password"},
            {"state": "email_verification", "url": "https://auth.openai.com/email-verification"},
        ])
        with patch.object(registration, "_browser_use_heartbeat", return_value=page), \
             patch.object(registration, "_quick_auth_state", side_effect=lambda _: next(states)), \
             patch.object(registration, "_click_use_password_if_present", return_value=True) as use_password, \
             patch.object(registration, "_fill_first", return_value=True), \
             patch.object(registration, "_click_first", return_value=True), \
             patch.object(registration, "_click_passwordless_signup_if_present") as passwordless, \
             patch.object(registration, "_registration_password", return_value="Password!1234"), \
             patch.object(registration, "_bu_delay"):
            result = registration._fill_password_if_present(
                page,
                "user@example.test",
                timeout=1,
                force=True,
            )
        self.assertEqual(result, "Password!1234")
        use_password.assert_called_once_with(page)
        passwordless.assert_not_called()

    def test_force_password_rejects_email_verification_without_password_control(self):
        page = Mock()
        with patch.object(registration, "_browser_use_heartbeat", return_value=page), \
             patch.object(registration, "_quick_auth_state", return_value={
                 "state": "email_verification",
                 "url": "https://auth.openai.com/email-verification",
             }), \
             patch.object(registration, "_click_use_password_if_present", return_value=False), \
             patch.object(registration, "_click_passwordless_signup_if_present") as passwordless:
            with self.assertRaisesRegex(RuntimeError, "Use password"):
                registration._fill_password_if_present(page, "user@example.test", timeout=1, force=True)
        passwordless.assert_not_called()

    def test_force_password_retry_keeps_existing_password_on_email_verification(self):
        page = Mock()
        with patch.object(registration, "_browser_use_heartbeat", return_value=page), \
             patch.object(registration, "_quick_auth_state", return_value={
                 "state": "email_verification",
                 "url": "https://auth.openai.com/email-verification",
             }), \
             patch.object(registration, "_click_use_password_if_present") as use_password:
            result = registration._fill_password_if_present(
                page,
                "user@example.test",
                timeout=1,
                force=True,
                password_already_set=True,
            )
        self.assertIsNone(result)
        use_password.assert_not_called()

    @patch.object(registration, "_wait_for_otp_with_browser_heartbeat", return_value="654321")
    def test_browser_context_2fa_flow(self, _wait_otp):
        request = _Request()
        context = _Context(request)
        page = Mock()
        page.evaluate.side_effect = lambda script: "en-US" if "navigator.language" in script else "Mozilla/5.0"

        secret = registration._setup_2fa_with_browser_context(
            context,
            page,
            "user@example.test",
            access_token="old-token",
        )

        self.assertEqual(secret, "JBSWY3DPEHPK3PXP")
        urls = [url for _method, url, _kwargs in request.calls]
        self.assertIn("https://chatgpt.com/api/auth/csrf", urls)
        self.assertTrue(any("/accounts/mfa/enroll" in url for url in urls))
        activate = next(kwargs for method, url, kwargs in request.calls if method == "post" and url.endswith("/activate_enrollment"))
        self.assertEqual(json.loads(activate["data"])["session_id"], "enroll-1")
        self.assertEqual(activate["headers"]["authorization"], "Bearer fresh-access-token")
        _wait_otp.assert_called_once()

    @patch.object(registration, "_wait_for_otp_with_browser_heartbeat", return_value="654321")
    def test_browser_context_2fa_activation_failure_is_fatal(self, _wait_otp):
        request = _Request(activate_payload={"success": False})
        context = _Context(request)
        page = Mock()
        page.evaluate.return_value = "en-US"
        with self.assertRaises(RuntimeError):
            registration._setup_2fa_with_browser_context(context, page, "user@example.test")


if __name__ == "__main__":
    unittest.main()
