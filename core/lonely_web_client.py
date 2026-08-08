# -*- coding: utf-8 -*-
"""
孤独哥 CDK 网页版客户端。

和额度卡版（core/lonely_mail_client.py）都是 CDK，但接口完全不同：
    - 网页版不签名，两个公开接口即可
    - 一张卡只兑换一个邮箱（不是 N 次额度）

流程（实测确认）：
    1. POST /api/v1/redeem  {code}
       未兑换的卡 -> code=0, data={"type":"email"}（注意：不返回邮箱地址）
       已兑换的卡 -> code=2004 "卡密使用中"（不是错误，说明已绑定，直接进第 2 步）
    2. GET /api/v1/order/lookup?code=...&poll=true
       -> data.email.session.emailAddress / sessionId
          data.email.codes[] = [{id, code, receivedAt}, ...] 新的在前
       未兑换就调 -> code=2005 "卡密尚未兑换"

取码也走同一个 lookup，所以邮箱地址和验证码是一个接口出来的。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from config import email as _email_cfg

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://sms.iosmq.xyz"
REQUEST_TIMEOUT = 70  # poll=true 时服务端会挂起等新码，超时给宽一点

REDEEM_RETRY_TIMES = 5
REDEEM_RETRY_INTERVAL = 1.0

# 业务码：重试没意义的那些
CODE_OK = "0"
CODE_IN_USE = "2004"        # 卡密使用中 —— 已兑换过，不是失败
CODE_NOT_REDEEMED = "2005"  # 卡密尚未兑换 —— lookup 早于 redeem
_PERMANENT_CODES = {"1001", "1002", "2001", "2002", "2003", "2004", "2005"}

_CONTEXT_CACHE: dict[str, "LonelyWebAccount"] = {}


class LonelyWebError(RuntimeError):
    """孤独哥网页版异常。code 为业务错误码。"""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class LonelyWebAccount:
    email: str
    session_id: str
    card_code: str
    base_url: str = DEFAULT_BASE_URL


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _base_url() -> str:
    raw = str(getattr(_email_cfg, "LONELY_WEB_BASE_URL", "") or "").strip()
    return (raw or DEFAULT_BASE_URL).rstrip("/")


def _request(method: str, path: str, *, params=None, body=None, base_url=None, timeout=None) -> tuple[str, dict]:
    """
    调用网页版接口。返回 (业务码, data)。

    和额度卡版不同：这里不把非 0 码当异常抛，因为 2004/2005 是流程的一部分
    （2004 表示卡已绑定，该继续 lookup），由调用方判断。
    """
    url = (base_url or _base_url()).rstrip("/") + path
    try:
        resp = requests.request(
            method,
            url,
            params=params,
            json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout or REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise LonelyWebError(f"孤独哥网页版请求失败 ({path}): {type(exc).__name__}: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise LonelyWebError(
            f"孤独哥网页版响应不是 JSON ({path}): HTTP {resp.status_code}; {(resp.text or '')[:160]}"
        ) from exc
    if not isinstance(payload, dict):
        raise LonelyWebError(f"孤独哥网页版响应格式异常 ({path}): {str(payload)[:160]}")

    code = str(payload.get("code") if payload.get("code") is not None else "")
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    if code != CODE_OK:
        data["_msg"] = payload.get("msg") or payload.get("message") or ""
    return code, data


def _retry(label: str, fn, times: int = REDEEM_RETRY_TIMES, interval: float = REDEEM_RETRY_INTERVAL):
    """重试 times 次、间隔 interval 秒；永久性业务错误直接抛。"""
    last_exc: Exception | None = None
    for attempt in range(1, max(1, times) + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if isinstance(exc, LonelyWebError) and exc.code in _PERMANENT_CODES:
                raise
            if attempt >= times:
                break
            logger.warning(
                "[LonelyWeb] %s 第 %s/%s 次失败: %s: %s，%.0fs 后重试",
                label, attempt, times, type(exc).__name__, exc, interval,
            )
            time.sleep(interval)
    raise LonelyWebError(f"孤独哥网页版 {label} 重试 {times} 次仍失败: {last_exc}") from last_exc


def redeem_card(card_code: str, base_url: str | None = None) -> str:
    """
    兑换卡密。返回状态：
        "redeemed" 首次兑换成功
        "in_use"   卡已兑换过（2004），可以直接 lookup
    其它业务错误抛异常。注意 redeem 不返回邮箱地址，要靠 lookup 拿。
    """
    code_str = str(card_code or "").strip()
    if not code_str:
        raise LonelyWebError("孤独哥网页版卡密为空")

    def _do():
        code, data = _request("POST", "/api/v1/redeem", body={"code": code_str})
        if code == CODE_OK:
            return "redeemed"
        if code == CODE_IN_USE:
            return "in_use"
        raise LonelyWebError(
            f"孤独哥网页版兑换失败: code={code} msg={data.get('_msg')}", code=code,
        )

    status = _retry(f"兑换卡密 {code_str}", _do)
    logger.info("[LonelyWeb] 卡密 %s 兑换状态: %s", code_str, status)
    return status


def lookup_order(card_code: str, poll: bool = True, base_url: str | None = None, timeout: int | None = None) -> dict:
    """
    查订单：一次拿到邮箱地址 + 验证码列表。

    poll=true 时服务端会挂起等新验证码，所以取码轮询用它；
    只想读当前状态（比如导入后回显邮箱）用 poll=false，返回快。
    """
    code_str = str(card_code or "").strip()
    code, data = _request(
        "GET",
        "/api/v1/order/lookup",
        params={"code": code_str, "poll": "true" if poll else "false"},
        base_url=base_url,
        timeout=timeout,
    )
    if code != CODE_OK:
        raise LonelyWebError(
            f"孤独哥网页版查单失败: code={code} msg={data.get('_msg')}", code=code,
        )

    email_block = data.get("email")
    if not isinstance(email_block, dict):
        raise LonelyWebError(f"孤独哥网页版查单响应缺少 email 块: {str(data)[:160]}")
    session = email_block.get("session")
    if not isinstance(session, dict):
        session = {}
    codes = email_block.get("codes")
    if not isinstance(codes, list):
        codes = []

    return {
        "email": str(session.get("emailAddress") or "").strip(),
        "session_id": str(session.get("sessionId") or "").strip(),
        "product_name": session.get("productName"),
        "status": session.get("status"),
        "expires_at": session.get("expiresAt"),
        "is_microsoft_oauth": bool(session.get("isMicrosoftOauth")),
        "email_source_type": session.get("emailSourceType"),
        "codes": [c for c in codes if isinstance(c, dict)],
    }


def pick_account() -> LonelyWebAccount:
    """
    领一张未兑换的卡，兑换出邮箱。

    一张卡只对一个邮箱，所以卡被领走就标 used，和额度卡版不同。
    """
    from core import db

    card = db.claim_next_lonely_web_card()
    if card is None:
        summary = db.lonely_web_card_pool_summary()
        raise LonelyWebError(
            f"孤独哥网页版 CDK 池没有可用卡密: {summary}. 请在 WebUI 邮箱池导入（每行一个卡密）"
        )

    card_code = card["card_code"]
    base = card.get("base_url") or _base_url()
    try:
        redeem_card(card_code, base_url=base)
        # redeem 不返回地址，必须再 lookup；这里 poll=false 只读当前状态。
        info = lookup_order(card_code, poll=False, base_url=base, timeout=30)
    except Exception as exc:
        db.release_lonely_web_card(card_code, status="available", note=f"兑换失败: {exc}")
        raise

    email = info["email"]
    if not email or "@" not in email:
        db.release_lonely_web_card(card_code, status="failed", note="查单未返回邮箱地址")
        raise LonelyWebError(f"孤独哥网页版查单未返回邮箱地址: {card_code}")

    account = LonelyWebAccount(
        email=email,
        session_id=info["session_id"],
        card_code=card_code,
        base_url=base,
    )
    _CONTEXT_CACHE[_cache_key(email)] = account

    db.record_lonely_web_redeemed_email(
        card_code=card_code,
        email=email,
        session_id=info["session_id"],
        product_name=info.get("product_name"),
        session_expires_at=info.get("expires_at"),
    )
    logger.info(
        "[LonelyWeb] 已兑换邮箱: %s sessionId=%s card=%s 会话到期=%s",
        email, info["session_id"], card_code, info.get("expires_at"),
    )
    return account


def get_account_context(email: str) -> LonelyWebAccount | None:
    key = _cache_key(email)
    if key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[key]

    from core import db
    row = db.get_lonely_web_redeemed_email(email)
    if not row:
        return None
    card = db.get_lonely_web_card(row.get("card_code") or "")
    account = LonelyWebAccount(
        email=row["email"],
        session_id=str(row.get("session_id") or ""),
        card_code=str(row.get("card_code") or ""),
        base_url=str((card or {}).get("base_url") or _base_url()),
    )
    _CONTEXT_CACHE[key] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """卡已消耗，邮箱退不回去，只更新本地记录。"""
    from core import db
    db.release_lonely_web_redeemed_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(_cache_key(email), None)


def _parse_ts(value) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).timestamp()
        except ValueError:
            pass
    return None


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    轮询 lookup 拿最新验证码。

    codes[] 里新的在前，但一张卡可能留着上一轮的旧码（实测那张已用卡里有 4 条），
    所以必须按 receivedAt 和 after_ts 过滤，否则会拿到上一次注册的码。
    """
    account = get_account_context(email)
    if account is None:
        raise LonelyWebError(f"孤独哥网页版邮箱不存在或未兑换: {email}")

    wait_seconds = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + max(0, wait_seconds)

    best_otp: str | None = None
    best_ts = float("-inf")
    settle_until: float | None = None
    last_error = "codes 为空，验证码还没到"

    logger.info(
        "[LonelyWeb] 开始轮询取码: %s card=%s 最长 %ss settle=%ss",
        email, account.card_code, wait_seconds, settle,
    )
    while time.monotonic() <= deadline:
        try:
            remaining_wait = max(1, int(deadline - time.monotonic()))
            # poll=true 让服务端挂起等新码，省掉空转；超时不超过剩余预算。
            info = lookup_order(
                account.card_code,
                poll=True,
                base_url=account.base_url,
                timeout=min(REQUEST_TIMEOUT, remaining_wait + 10),
            )
            for item in info["codes"]:
                otp = str(item.get("code") or "").strip()
                if not otp:
                    continue
                msg_ts = _parse_ts(item.get("receivedAt") or item.get("received_at"))
                if after_ts is not None and msg_ts is not None and msg_ts < after_ts - 30:
                    continue
                candidate_ts = float("-inf") if msg_ts is None else msg_ts
                if best_otp is None or candidate_ts > best_ts or (candidate_ts == best_ts and otp != best_otp):
                    if best_otp and otp != best_otp:
                        logger.info("[LonelyWeb] 发现更新验证码 %s，替换 %s，重置 settle", otp, best_otp)
                    else:
                        logger.info("[LonelyWeb] 锁定验证码候选 %s，等 %ss 确认", otp, settle)
                    best_otp = otp
                    best_ts = candidate_ts
                    settle_until = time.monotonic() + settle

            now = time.monotonic()
            if best_otp and settle_until is not None and now >= settle_until:
                logger.info("[LonelyWeb] settle 完成，返回验证码 %s", best_otp)
                return best_otp
        except LonelyWebError as exc:
            last_error = str(exc)
            # 卡失效/未兑换这类永久错误，继续轮询也没用
            if exc.code in _PERMANENT_CODES:
                raise
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_otp:
        logger.warning("[LonelyWeb] 总超时但已有候选，返回验证码 %s", best_otp)
        return best_otp
    raise LonelyWebError(f"等待孤独哥网页版验证码超时: {email}; {last_error}")
