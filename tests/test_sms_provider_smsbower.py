# -*- coding: utf-8 -*-
import time
import unittest
from unittest.mock import patch

from config import codex as codex_config
from config import env_loader
from core import sms_provider
from webui import config_editor


class _Response:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        response = self.responses.pop(0)
        if isinstance(response, tuple):
            return _Response(response[1], response[0])
        return _Response(response)

    def close(self):
        self.closed = True


def _smsbower_config(**overrides):
    values = {
        "SMS_PROVIDER": "smsbower",
        "SMSBOWER_API_BASE": "https://smsbower.page/stubs/handler_api.php",
        "SMSBOWER_API_KEY": "bower-key",
        "SMS_SERVICE": "go",
        "SMS_COUNTRY": "2",
        "SMS_MAX_PRICE": "3.50",
    }
    values.update(overrides)
    return patch.multiple(codex_config, **values)


class SMSBowerProviderTests(unittest.TestCase):
    def tearDown(self):
        sms_provider._ACQUIRED_AT.clear()

    def test_config_and_webui_include_smsbower_fields(self):
        self.assertIn("SMSBOWER_API_KEY", env_loader.SECRET_ENV_KEYS)
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}
        self.assertEqual(fields["SMSBOWER_API_BASE"]["storage"], "env")
        self.assertEqual(fields["SMSBOWER_API_KEY"]["storage"], "env")
        self.assertTrue(fields["SMSBOWER_API_KEY"]["secret"])

    def test_acquire_number_uses_smsbower_handler_api(self):
        http = _Http(["ACCESS_NUMBER:123456:+12025550123"])

        with _smsbower_config():
            activation_id, phone = sms_provider.acquire_number(http=http)

        self.assertEqual((activation_id, phone), ("123456", "12025550123"))
        self.assertEqual(http.calls[0]["url"], "https://smsbower.page/stubs/handler_api.php")
        self.assertEqual(
            http.calls[0]["params"],
            {
                "api_key": "bower-key",
                "action": "getNumber",
                "service": "go",
                "country": "2",
                "maxPrice": "3.50",
            },
        )

    def test_historical_openai_service_value_maps_to_smsbower_code(self):
        http = _Http(["ACCESS_NUMBER:123456:12025550123"])

        with _smsbower_config(SMS_SERVICE="openai"):
            sms_provider.acquire_number(http=http)

        self.assertEqual(http.calls[0]["params"]["service"], "dr")

    def test_wait_for_sms_code_polls_until_status_ok(self):
        http = _Http(["STATUS_WAIT_CODE", "STATUS_OK:847291"])

        with _smsbower_config(), patch.object(sms_provider.time, "sleep"):
            code = sms_provider.wait_for_sms_code(
                "123456", http=http, max_wait=5, poll_interval=1
            )

        self.assertEqual(code, "847291")
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(
            http.calls[0]["params"],
            {"api_key": "bower-key", "action": "getStatus", "id": "123456"},
        )

    def test_complete_sets_status_six(self):
        http = _Http(["ACCESS_ACTIVATION"])
        sms_provider._ACQUIRED_AT["123456"] = time.time()

        with _smsbower_config():
            sms_provider.complete("123456", http=http)

        self.assertEqual(
            http.calls[0]["params"],
            {
                "api_key": "bower-key",
                "action": "setStatus",
                "status": "6",
                "id": "123456",
            },
        )
        self.assertNotIn("123456", sms_provider._ACQUIRED_AT)

    def test_cancel_sets_status_eight_after_minimum_age(self):
        http = _Http(["ACCESS_CANCEL"])
        sms_provider._ACQUIRED_AT["123456"] = time.time() - 130

        with _smsbower_config(), patch.object(sms_provider, "_http", return_value=http):
            sms_provider.cancel("123456", background=False)

        self.assertEqual(
            http.calls[0]["params"],
            {
                "api_key": "bower-key",
                "action": "setStatus",
                "status": "8",
                "id": "123456",
            },
        )
        self.assertTrue(http.closed)

    def test_common_smsbower_errors_are_mapped(self):
        cases = (
            ("NO_NUMBERS", sms_provider.SmsNoNumbersError),
            ("NO_BALANCE", sms_provider.SmsNoBalanceError),
            ("BAD_KEY", sms_provider.SmsProviderError),
            ("SERVER_ERROR", sms_provider.SmsProviderError),
        )
        with _smsbower_config():
            for response, error_type in cases:
                with self.subTest(response=response):
                    with self.assertRaises(error_type):
                        sms_provider.acquire_number(http=_Http([response]))

    def test_http_401_is_reported_as_invalid_api_key(self):
        with _smsbower_config():
            with self.assertRaisesRegex(sms_provider.SmsProviderError, "HTTP 401"):
                sms_provider.acquire_number(http=_Http([(401, '{"message":"No access"}')]))

    def test_early_cancel_error_is_reported(self):
        with _smsbower_config():
            with self.assertRaisesRegex(
                sms_provider.SmsProviderError, "EARLY_CANCEL_DENIED"
            ):
                sms_provider.set_status(
                    "123456", 8, http=_Http(["EARLY_CANCEL_DENIED"])
                )

    def test_missing_api_key_is_reported_before_request(self):
        http = _Http([])
        with _smsbower_config(SMSBOWER_API_KEY=""):
            with self.assertRaisesRegex(
                sms_provider.SmsProviderError, "SMSBOWER_API_KEY"
            ):
                sms_provider.acquire_number(http=http)
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
