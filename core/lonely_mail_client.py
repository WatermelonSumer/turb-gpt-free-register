# -*- coding: utf-8 -*-
"""
孤独哥 API 邮箱客户端（CDK 兑换制）。

与其它来源的区别：邮箱池里存的是 CDK 卡密，不是邮箱地址。
    1. 导入 CDK（卡密），一张卡有 N 次额度；
    2. 注册时 pick_account()：激活卡密拿 apiKey/apiSecret，再创建邮箱会话，
       服务端分配一个邮箱地址并返回 sessionId；
    3. 取码时按 sessionId 轮询 codes[]。

一张 CDK 可以兑换出多个邮箱，所以 CDK 池和「已兑换邮箱」是一对多关系。
已兑换的邮箱会写回 DB，前端邮箱池页面据此回显。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import requests

from config import email as _email_cfg

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://sms.iosmq.xyz"
REQUEST_TIMEOUT = 20
# 空 body 的 SHA256，GET 请求签名用
_EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

# 兑换（激活 + 创建会话）失败时的重试策略
ACTIVATE_RETRY_TIMES = 5
ACTIVATE_RETRY_INTERVAL = 1.0

_CONTEXT_CACHE: dict[str, "LonelyMailAccount"] = {}


class LonelyMailError(RuntimeError):
    """孤独哥 API 相关异常。code 为业务错误码，网络/解析类错误为 None。"""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


# 这些业务错误重试没有意义：卡不存在 / 额度用完 / 已过期 / 卡类型不对。
_PERMANENT_CODES = {"2001", "2002", "2003", "2005"}


@dataclass
class LonelyMailAccount:
    email: str
    session_id: str
    card_code: str
    api_key: str
    api_secret: str
    base_url: str = DEFAULT_BASE_URL


def _cache_key(email: str) -> str:
    return str(email or "").strip().lower()


def _base_url() -> str:
    raw = str(getattr(_email_cfg, "LONELY_MAIL_BASE_URL", "") or "").strip()
    return (raw or DEFAULT_BASE_URL).rstrip("/")


def _sign(method: str, path: str, body_text: str, api_secret: str) -> tuple[str, str]:
    """
    按文档计算签名：
        signText = METHOD + "\\n" + PATH + "\\n" + TIMESTAMP + "\\n" + SHA256(BODY)
        signature = HMAC_SHA256(apiSecret, signText)

    PATH 只含路径，不含域名；GET 的 body 为空串。
    返回 (timestamp, signature)。
    """
    timestamp = str(int(time.time() * 1000))
    body_hash = (
        _EMPTY_BODY_SHA256 if not body_text
        else hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    )
    sign_text = "\n".join([method.upper(), path, timestamp, body_hash])
    signature = hmac.new(
        api_secret.encode("utf-8"),
        sign_text.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return timestamp, signature


def _request(
    method: str,
    path: str,
    *,
    api_key: str = "",
    api_secret: str = "",
    body: dict | None = None,
    base_url: str | None = None,
) -> dict:
    """
    调用孤独哥外部 API。带 api_secret 时自动加签名头。

    注意：签名里的 bodyHash 必须和实际发出的字节完全一致，
    所以这里手动序列化 body 再用 data= 发送，不能用 requests 的 json=。
    """
    url = (base_url or _base_url()).rstrip("/") + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body else ""

    if api_secret:
        timestamp, signature = _sign(method, path, body_text, api_secret)
        headers["X-API-Key"] = api_key
        headers["X-Timestamp"] = timestamp
        headers["X-Signature"] = signature

    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            data=body_text.encode("utf-8") if body_text else None,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise LonelyMailError(f"孤独哥 API 请求失败 ({path}): {type(exc).__name__}: {exc}") from exc

    try:
        payload = resp.json()
    except ValueError as exc:
        raise LonelyMailError(
            f"孤独哥 API 响应不是 JSON ({path}): HTTP {resp.status_code}; {(resp.text or '')[:160]}"
        ) from exc

    if not isinstance(payload, dict):
        raise LonelyMailError(f"孤独哥 API 响应格式异常 ({path}): {str(payload)[:160]}")

    code = str(payload.get("code") if payload.get("code") is not None else "")
    if code != "0":
        msg = payload.get("msg") or payload.get("message") or ""
        raise LonelyMailError(f"孤独哥 API 返回错误 ({path}): code={code} msg={msg}", code=code)

    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _retry(label: str, fn, times: int = ACTIVATE_RETRY_TIMES, interval: float = ACTIVATE_RETRY_INTERVAL):
    """重试 times 次，每次间隔 interval 秒；全部失败则抛最后一个异常。"""
    last_exc: Exception | None = None
    for attempt in range(1, max(1, times) + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            # 永久性业务错误直接抛，重试只会白等。
            if isinstance(exc, LonelyMailError) and exc.code in _PERMANENT_CODES:
                raise
            if attempt >= times:
                break
            logger.warning(
                "[LonelyMail] %s 第 %s/%s 次失败: %s: %s，%.0fs 后重试",
                label, attempt, times, type(exc).__name__, exc, interval,
            )
            time.sleep(interval)
    raise LonelyMailError(f"孤独哥 {label} 重试 {times} 次仍失败: {last_exc}") from last_exc


def activate_card(card_code: str, base_url: str | None = None) -> dict:
    """
    激活卡密，拿 apiKey / apiSecret / 额度。重复激活返回同一组密钥，不扣次数。
    返回原始 data，包含 apiKey/apiSecret/productName/totalQuota/usedQuota/remainingQuota/expiresAt。
    """
    code = str(card_code or "").strip()
    if not code:
        raise LonelyMailError("孤独哥卡密为空")

    data = _retry(
        f"激活卡密 {code}",
        lambda: _request(
            "POST",
            "/api/v1/external/cards/activate",
            body={"cardCode": code},
            base_url=base_url,
        ),
    )
    api_key = str(data.get("apiKey") or "").strip()
    api_secret = str(data.get("apiSecret") or "").strip()
    if not api_key or not api_secret:
        raise LonelyMailError(f"孤独哥激活响应缺少 apiKey/apiSecret: {str(data)[:160]}")
    logger.info(
        "[LonelyMail] 卡密激活成功: %s product=%s 额度 %s/%s 剩余=%s 到期=%s",
        code, data.get("productName"), data.get("usedQuota"),
        data.get("totalQuota"), data.get("remainingQuota"), data.get("expiresAt"),
    )
    return data


def query_quota(api_key: str, api_secret: str, base_url: str | None = None) -> dict:
    """查询额度，不扣次数。"""
    return _request(
        "GET",
        "/api/v1/external/quota",
        api_key=api_key,
        api_secret=api_secret,
        base_url=base_url,
    )


def create_session(
    api_key: str,
    api_secret: str,
    external_session_no: str | None = None,
    base_url: str | None = None,
) -> dict:
    """
    创建邮箱会话，扣 1 次额度，服务端分配邮箱地址。
    返回 data，包含 sessionId/emailAddress/status/expiresAt/remainingQuota。
    """
    body: dict = {}
    if external_session_no:
        body["externalSessionNo"] = str(external_session_no)

    data = _retry(
        "创建邮箱会话",
        lambda: _request(
            "POST",
            "/api/v1/external/email/sessions",
            api_key=api_key,
            api_secret=api_secret,
            body=body or None,
            base_url=base_url,
        ),
    )
    email = str(data.get("emailAddress") or "").strip()
    session_id = str(data.get("sessionId") or "").strip()
    if not email or "@" not in email or not session_id:
        raise LonelyMailError(f"孤独哥创建会话响应缺少 emailAddress/sessionId: {str(data)[:160]}")
    logger.info(
        "[LonelyMail] 已兑换邮箱: %s sessionId=%s 剩余额度=%s 会话到期=%s",
        email, session_id, data.get("remainingQuota"), data.get("expiresAt"),
    )
    return data


def pick_account() -> LonelyMailAccount:
    """
    从 CDK 池领一张还有额度的卡，兑换出一个邮箱。

    流程：claim 卡 → 激活（幂等，已激活的直接复用库里的密钥）→ 创建会话 →
    把兑换出来的邮箱写回 DB（前端邮箱池页面据此回显）。
    """
    from core import db

    card = db.claim_next_lonely_card()
    if card is None:
        summary = db.lonely_card_pool_summary()
        raise LonelyMailError(
            f"孤独哥 CDK 池没有可用卡密: {summary}. 请在 WebUI 邮箱池导入 CDK（每行一个卡密）"
        )

    card_code = card["card_code"]
    base = card.get("base_url") or _base_url()
    api_key = str(card.get("api_key") or "").strip()
    api_secret = str(card.get("api_secret") or "").strip()

    try:
        # 没缓存密钥时才激活；同一张卡重复激活返回同一组密钥，不扣次数。
        if not api_key or not api_secret:
            info = activate_card(card_code, base_url=base)
            api_key = str(info.get("apiKey") or "")
            api_secret = str(info.get("apiSecret") or "")
            db.update_lonely_card_activation(
                card_code,
                api_key=api_key,
                api_secret=api_secret,
                product_name=info.get("productName"),
                total_quota=info.get("totalQuota"),
                used_quota=info.get("usedQuota"),
                remaining_quota=info.get("remainingQuota"),
                expires_at=info.get("expiresAt"),
            )

        session = create_session(api_key, api_secret, base_url=base)
    except Exception as exc:
        # 兑换失败不扣额度，把卡放回池子，附上原因。
        db.release_lonely_card(card_code, status="available", note=f"兑换失败: {exc}")
        raise

    email = str(session.get("emailAddress") or "").strip()
    session_id = str(session.get("sessionId") or "").strip()

    account = LonelyMailAccount(
        email=email,
        session_id=session_id,
        card_code=card_code,
        api_key=api_key,
        api_secret=api_secret,
        base_url=base,
    )
    _CONTEXT_CACHE[_cache_key(email)] = account

    # 关键：把兑换结果落库，前端邮箱池 / 账号页才能看到这个邮箱。
    db.record_lonely_redeemed_email(
        card_code=card_code,
        email=email,
        session_id=session_id,
        remaining_quota=session.get("remainingQuota"),
        session_expires_at=session.get("expiresAt"),
    )
    return account


def get_account_context(email: str) -> LonelyMailAccount | None:
    key = _cache_key(email)
    if key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[key]

    from core import db
    row = db.get_lonely_redeemed_email(email)
    if not row:
        return None
    card = db.get_lonely_card(row.get("card_code") or "")
    if not card:
        return None
    account = LonelyMailAccount(
        email=row["email"],
        session_id=str(row.get("session_id") or ""),
        card_code=str(row.get("card_code") or ""),
        api_key=str(card.get("api_key") or ""),
        api_secret=str(card.get("api_secret") or ""),
        base_url=str(card.get("base_url") or _base_url()),
    )
    _CONTEXT_CACHE[key] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """
    释放已兑换邮箱。

    注意：额度已经扣掉了，邮箱不可能退回服务端，所以这里只更新本地记录状态，
    不把 CDK 卡改回 available（卡的可用性由剩余额度决定）。
    """
    from core import db
    db.release_lonely_redeemed_email(email, status=status, note=note)
    _CONTEXT_CACHE.pop(_cache_key(email), None)


def _parse_ts(value) -> float | None:
    """解析 receivedAt，兼容 ISO8601（含 Z）和常见字符串格式。"""
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


def query_session(account: LonelyMailAccount) -> dict:
    """查询会话详情（含 codes[]），不扣次数。"""
    return _request(
        "GET",
        f"/api/v1/external/email/sessions/{account.session_id}",
        api_key=account.api_key,
        api_secret=account.api_secret,
        base_url=account.base_url,
    )


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    轮询会话的 codes[]，返回 after_ts 之后最新的验证码。

    settle 机制与其它来源一致：首次拿到码后再等 settle 秒，
    期间出现更新的码就替换并重置计时，避免拿到旧码。
    """
    account = get_account_context(email)
    if account is None:
        raise LonelyMailError(f"孤独哥邮箱不存在或未兑换: {email}")
    if not account.session_id:
        raise LonelyMailError(f"孤独哥邮箱缺少 sessionId，无法取码: {email}")

    wait_seconds = int(max_wait if max_wait is not None else _email_cfg.OTP_MAX_WAIT)
    interval = max(1, int(poll_interval if poll_interval is not None else _email_cfg.OTP_POLL_INTERVAL))
    settle = max(0, int(settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS))
    deadline = time.monotonic() + max(0, wait_seconds)

    best_otp: str | None = None
    best_ts = float("-inf")
    settle_until: float | None = None
    last_error = "会话中尚未出现验证码"

    logger.info(
        "[LonelyMail] 开始轮询会话取码: %s sessionId=%s 最长 %ss settle=%ss",
        email, account.session_id, wait_seconds, settle,
    )
    while time.monotonic() <= deadline:
        try:
            data = query_session(account)
            codes = data.get("codes")
            if not isinstance(codes, list):
                codes = []
            if not codes:
                last_error = "codes 为空，验证码还没到"

            for item in codes:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("code") or "").strip()
                if not code:
                    continue
                msg_ts = _parse_ts(item.get("receivedAt") or item.get("received_at"))
                # 服务端时间可能有几秒偏差，留 30s 容差再判定为旧码。
                if after_ts is not None and msg_ts is not None and msg_ts < after_ts - 30:
                    continue
                candidate_ts = float("-inf") if msg_ts is None else msg_ts
                if best_otp is None or candidate_ts > best_ts or (candidate_ts == best_ts and code != best_otp):
                    if best_otp and code != best_otp:
                        logger.info("[LonelyMail] 发现更新验证码 %s，替换 %s，重置 settle", code, best_otp)
                    else:
                        logger.info("[LonelyMail] 锁定验证码候选 %s，等 %ss 确认", code, settle)
                    best_otp = code
                    best_ts = candidate_ts
                    settle_until = time.monotonic() + settle

            now = time.monotonic()
            if best_otp and settle_until is not None and now >= settle_until:
                logger.info("[LonelyMail] settle 完成，返回验证码 %s", best_otp)
                return best_otp
        except LonelyMailError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))

    if best_otp:
        logger.warning("[LonelyMail] 总超时但已有候选，返回验证码 %s", best_otp)
        return best_otp
    raise LonelyMailError(f"等待孤独哥验证码超时: {email}; {last_error}")
