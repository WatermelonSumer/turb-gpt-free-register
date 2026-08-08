# -*- coding: utf-8 -*-
"""手动 OTP 通道回归测试。

历史 bug：等待循环内联调用 input()，在 stdin 是 TTY 的环境（python web.py
直接在终端启动）会永久阻塞注册线程，WebUI 提交的验证码永远没人消费。
"""
import builtins
import sys
import threading
import time
import unittest
from unittest.mock import patch

from core import manual_otp

EMAIL = "manual-otp-test@example.com"


class _FakeStdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class ManualOtpTests(unittest.TestCase):
    def setUp(self):
        manual_otp._codes.clear()
        manual_otp._events.clear()
        manual_otp._waiting.clear()
        manual_otp._stdin_enabled = False
        manual_otp._stdin_reader_started = False

    def _wait_in_thread(self, result: dict, timeout: int = 10):
        def worker():
            try:
                result["code"] = manual_otp.wait_for_manual_otp(EMAIL, timeout=timeout)
            except BaseException as exc:  # noqa: BLE001 - 测试要看到任何异常
                result["error"] = f"{type(exc).__name__}: {exc}"

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        return t

    def _await_waiting(self, timeout: float = 3.0):
        end = time.time() + timeout
        while time.time() < end:
            if manual_otp.list_waiting():
                return True
            time.sleep(0.05)
        return False

    def test_webui_submit_consumed_even_when_stdin_is_a_tty(self):
        """核心回归：stdin 是 TTY 时不得阻塞在 input() 上。"""
        def boom(prompt=""):
            raise AssertionError("等待循环不应调用 input()")

        result = {}
        with patch.object(sys, "stdin", _FakeStdin(True)), patch.object(builtins, "input", boom):
            t = self._wait_in_thread(result)
            self.assertTrue(self._await_waiting(), "任务未进入等待状态")
            manual_otp.submit_manual_otp(EMAIL, "123456")
            t.join(timeout=5)

        self.assertFalse(t.is_alive(), "等待线程卡死，验证码没被消费")
        self.assertEqual(result.get("code"), "123456", result)

    def test_precommitted_code_returns_immediately(self):
        manual_otp.submit_manual_otp(EMAIL, "654321")
        with patch.object(sys, "stdin", _FakeStdin(False)):
            self.assertEqual(manual_otp.wait_for_manual_otp(EMAIL, timeout=10), "654321")
        self.assertEqual(manual_otp.list_waiting(), [])

    def test_cli_stdin_fallback_still_works_when_enabled(self):
        manual_otp.enable_stdin_fallback()
        typed = ["112233"]

        def fake_input(prompt=""):
            if typed:
                return typed.pop(0)
            time.sleep(0.2)
            return ""

        result = {}
        with patch.object(sys, "stdin", _FakeStdin(True)), patch.object(builtins, "input", fake_input):
            t = self._wait_in_thread(result)
            t.join(timeout=8)

        self.assertFalse(t.is_alive(), "CLI 终端输入未被消费")
        self.assertEqual(result.get("code"), "112233", result)

    def test_stop_request_is_not_swallowed(self):
        from core import registration_service as svc

        result = {}
        with patch.object(sys, "stdin", _FakeStdin(False)), \
                patch.object(svc, "check_stop_requested", side_effect=svc.StopRequested("任务 #1 已被用户手动停止")):
            t = self._wait_in_thread(result)
            t.join(timeout=8)

        self.assertFalse(t.is_alive(), "停止信号未能中断等待")
        self.assertIn("StopRequested", result.get("error", ""), result)
        self.assertEqual(manual_otp.list_waiting(), [])

    def test_email_provider_manual_mode_never_touches_stdin(self):
        """真实注册路径：run_registration -> wait_for_otp -> 手动通道。

        历史 bug：main.py 在 USE_EMAIL_SERVICE=False 时直接 input()，
        绕过手动通道，把 WebUI 的注册线程阻塞在 stdin 上。
        """
        from config import email as email_cfg
        from core import email_provider

        def boom(prompt=""):
            raise AssertionError("手动模式不应调用 input()")

        result = {}

        def worker():
            try:
                result["code"] = email_provider.wait_for_otp(EMAIL, after_ts=time.time(), max_wait=10)
            except BaseException as exc:  # noqa: BLE001
                result["error"] = f"{type(exc).__name__}: {exc}"

        with patch.object(email_cfg, "USE_EMAIL_SERVICE", False), \
                patch.object(sys, "stdin", _FakeStdin(True)), \
                patch.object(builtins, "input", boom):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            self.assertTrue(self._await_waiting(), "wait_for_otp 未进入手动等待状态")
            manual_otp.submit_manual_otp(EMAIL, "445566")
            t.join(timeout=5)

        self.assertFalse(t.is_alive(), "wait_for_otp 卡死")
        self.assertEqual(result.get("code"), "445566", result)

    def test_waiting_metadata_exposes_job_id(self):
        result = {}
        with patch.object(sys, "stdin", _FakeStdin(False)):
            def worker():
                try:
                    result["code"] = manual_otp.wait_for_manual_otp(EMAIL, timeout=10, job_id=77)
                except BaseException as exc:  # noqa: BLE001
                    result["error"] = repr(exc)

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            self.assertTrue(self._await_waiting(), "任务未进入等待状态")
            waiting = manual_otp.list_waiting()
            self.assertEqual(waiting[0]["job_id"], 77)
            self.assertEqual(waiting[0]["email"], EMAIL)
            manual_otp.submit_manual_otp(EMAIL, "999888")
            t.join(timeout=5)

        self.assertEqual(result.get("code"), "999888", result)


if __name__ == "__main__":
    unittest.main()
