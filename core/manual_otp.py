# -*- coding: utf-8 -*-
"""非交互环境下的手动 OTP 通道（WebUI / 后台任务用）。

用法：
  1. 注册任务调用 wait_for_manual_otp(email)
  2. 用户在 WebUI 对任务提交 6 位验证码，或调用 submit_manual_otp(email, code)
  3. 等待侧拿到验证码后继续
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import defaultdict

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# email(lower) -> list[code]  支持同一邮箱多次验证码
_codes: dict[str, list[str]] = defaultdict(list)
# email(lower) -> Event
_events: dict[str, threading.Event] = {}
# email(lower) -> waiting meta
_waiting: dict[str, dict] = {}

# 终端输入兜底：仅 CLI（main.py）显式开启。
# WebUI 绝不能开——阻塞的 input() 会占住注册线程，导致页面提交的验证码永远没人消费。
_stdin_enabled = False
_stdin_reader_started = False


def _norm(email: str) -> str:
    return str(email or "").strip().lower()


def _event_for(email: str) -> threading.Event:
    key = _norm(email)
    ev = _events.get(key)
    if ev is None:
        ev = threading.Event()
        _events[key] = ev
    return ev


def mark_waiting(email: str, job_id: int | None = None) -> None:
    key = _norm(email)
    with _lock:
        _waiting[key] = {
            "email": email,
            "job_id": job_id,
            "since": time.time(),
        }
        _event_for(key).clear()


def clear_waiting(email: str) -> None:
    key = _norm(email)
    with _lock:
        _waiting.pop(key, None)


def list_waiting() -> list[dict]:
    with _lock:
        return [dict(v) for v in _waiting.values()]


def submit_manual_otp(email: str, code: str) -> dict:
    key = _norm(email)
    code = str(code or "").strip().replace(" ", "")
    if not key:
        raise ValueError("email 为空")
    if not code:
        raise ValueError("验证码为空")
    if not code.isdigit() or len(code) not in (4, 5, 6, 7, 8):
        # OpenAI 通常 6 位；放宽一点兼容
        raise ValueError(f"验证码格式看起来不对: {code!r}")
    with _lock:
        _codes[key].append(code)
        _event_for(key).set()
    logger.info("[ManualOTP] 已提交验证码：email=%s code=%s", email, code)
    return {"ok": True, "email": email, "code": code}


def pop_manual_otp(email: str) -> str | None:
    key = _norm(email)
    with _lock:
        queue = _codes.get(key) or []
        if not queue:
            return None
        code = queue.pop(0)
        if not queue:
            _event_for(key).clear()
        return code


def enable_stdin_fallback(enabled: bool = True) -> None:
    """允许从终端读验证码。只有真正的交互式 CLI 才该调用。"""
    global _stdin_enabled
    _stdin_enabled = bool(enabled)


def _stdin_fallback_enabled() -> bool:
    if _stdin_enabled:
        return True
    return str(os.environ.get("MANUAL_OTP_STDIN", "")).strip().lower() in ("1", "true", "yes", "on")


def _oldest_waiting_email() -> str | None:
    """当前等待最久的邮箱。终端只有一个，多任务时按先来先服务派发。"""
    with _lock:
        if not _waiting:
            return None
        return min(_waiting.values(), key=lambda v: v.get("since") or 0).get("email")


def _stdin_reader_loop() -> None:
    """独立线程里读 stdin，读到就丢进队列。绝不阻塞等待侧。"""
    global _stdin_reader_started
    try:
        while _stdin_fallback_enabled():
            target = _oldest_waiting_email()
            if not target:
                time.sleep(0.5)
                continue
            try:
                typed = input(f">>> 手动输入 {target} 的邮箱验证码（回车提交）: ").strip()
            except Exception as exc:
                logger.info("[ManualOTP] stdin 不可读，终端输入兜底已停止：%s", exc)
                return
            if not typed:
                continue
            try:
                submit_manual_otp(_oldest_waiting_email() or target, typed)
            except ValueError as exc:
                logger.warning("[ManualOTP] 终端输入被拒绝：%s", exc)
    finally:
        with _lock:
            _stdin_reader_started = False


def _ensure_stdin_reader() -> None:
    global _stdin_reader_started
    if not _stdin_fallback_enabled():
        return
    try:
        if not (getattr(sys, "stdin", None) and sys.stdin.isatty()):
            return
    except Exception:
        return
    with _lock:
        if _stdin_reader_started:
            return
        _stdin_reader_started = True
    threading.Thread(target=_stdin_reader_loop, name="manual-otp-stdin", daemon=True).start()


def wait_for_manual_otp(email: str, *, timeout: int = 180, job_id: int | None = None) -> str:
    """阻塞等待手动验证码。优先吃已提交的 code，否则事件等待。"""
    key = _norm(email)
    if not key:
        raise RuntimeError("手动 OTP：email 为空")

    # 若已有预提交验证码，直接用
    existing = pop_manual_otp(email)
    if existing:
        clear_waiting(email)
        return existing

    mark_waiting(email, job_id=job_id)
    logger.info(
        "[ManualOTP] 等待手动输入验证码：email=%s timeout=%ss job=%s",
        email,
        timeout,
        job_id or "-",
    )
    logger.info("[ManualOTP] 请打开邮箱 %s，在 WebUI 任务旁提交 6 位验证码", email)

    # 终端输入兜底交给独立线程，等待循环只等事件，保证 WebUI 提交能被立刻消费
    _ensure_stdin_reader()

    end = time.time() + max(10, int(timeout))
    ev = _event_for(key)
    try:
        while time.time() < end:
            code = pop_manual_otp(email)
            if code:
                logger.info("[ManualOTP] 已取到验证码：email=%s code=%s", email, code)
                return code

            ev.wait(timeout=1.0)

            # 支持任务被手动停止（StopRequested 必须往上抛，不能吞）
            try:
                from core.registration_service import check_stop_requested
            except ImportError:
                pass
            else:
                check_stop_requested()
        raise TimeoutError(f"等待手动验证码超时（{timeout}s）：{email}")
    finally:
        clear_waiting(email)
