"""Public API; no console, database, mailbox provider, or SMS dependencies."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit


class ExtractionError(RuntimeError):
    """A safe-to-display extraction failure; never contains remote response bodies."""


@dataclass(frozen=True, slots=True)
class TokenBundle:
    email: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    id_token: str = field(repr=False)
    expires_in: int
    obtained_at: str
    expires_at: str
    chatgpt_account_id: str = ""
    chatgpt_user_id: str = ""
    plan_type: str = ""
    sub: str = ""
    token_type: str = "Bearer"

    def to_dict(self) -> dict:
        """Explicit credential export. Do not log this dictionary."""
        return asdict(self)


def _validate_proxy(proxy: str | None) -> None:
    if proxy is None:
        return
    try:
        parsed = urlsplit(proxy)
        if (parsed.scheme not in {"http", "https", "socks5", "socks5h"}
                or not parsed.hostname or parsed.fragment
                or parsed.query or parsed.path not in {"", "/"}):
            raise ValueError
        _ = parsed.port
    except (ValueError, TypeError):
        raise ValueError("代理必须为有效的 http/https/socks5/socks5h URL") from None


async def extract_tokens(
    email: str,
    password: str,
    *,
    totp_secret: str | None = None,
    totp_code: str | None = None,
    totp_fetcher: Callable[[], Awaitable[str]] | None = None,
    otp_fetcher: Callable[[str], Awaitable[str]] | None = None,
    proxy: str | None = None,
    timeout: float = 180,
    on_step: Callable[[str, str], None] | None = None,
) -> TokenBundle:
    """Log into one existing account and return all three tokens.

    Supply at most one of totp_secret, totp_code, or totp_fetcher. A fresh
    totp_fetcher is preferable to an already running six-digit code.
    Additional email verification uses otp_fetcher; phone requirements stop.
    No output files are written by this function.
    """
    if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+", email.strip()):
        raise ValueError("请输入有效的账号邮箱")
    if not isinstance(password, str) or not password:
        raise ValueError("账号密码不能为空")
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout 必须是大于零的有限秒数")
    if sum(value is not None for value in (totp_secret, totp_code, totp_fetcher)) > 1:
        raise ValueError("2FA 密钥、动态码和取码回调只能选择一种")
    _validate_proxy(proxy)
    for callback in (totp_fetcher, otp_fetcher, on_step):
        if callback is not None and not callable(callback):
            raise ValueError("验证码和进度回调必须可调用")

    # Lazy import keeps packaging/help/token storage independent of HTTP libraries.
    from ._protocol.login import ProtocolError, login_get_tokens
    from ._protocol.totp import normalize_totp_secret

    if totp_secret is not None:
        totp_secret = normalize_totp_secret(totp_secret)
    if totp_code is not None:
        if not isinstance(totp_code, str) or not re.fullmatch(r"[0-9]{6}", totp_code.strip()):
            raise ValueError("Authenticator 动态码必须是 6 位数字")
        code = totp_code.strip()

        async def one_time_code() -> str:
            return code

        totp_fetcher = one_time_code

    try:
        result = await asyncio.wait_for(
            login_get_tokens(
                email=email.strip(), password=password, totp_secret=totp_secret,
                totp_fetcher=totp_fetcher, otp_fetcher=otp_fetcher,
                proxy=proxy, on_step=on_step,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise ExtractionError("登录超时；未保存 Token，可检查网络后重试") from None
    except ProtocolError:
        raise
    except Exception as exc:
        # Network exception messages can include proxy credentials, URLs or bodies.
        raise ExtractionError(f"登录失败（{type(exc).__name__}）；请检查网络及账号验证状态") from None

    tokens = {key: getattr(result, key, None) for key in ("access_token", "refresh_token", "id_token")}
    missing = [key for key, value in tokens.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        raise ExtractionError("服务端未返回完整 Token：缺少 " + ", ".join(missing))
    try:
        expires_in = int(result.expires_in)
        if expires_in < 0:
            raise ValueError
        now = datetime.now(timezone.utc).replace(microsecond=0)
        expires_at = (now + timedelta(seconds=expires_in)).isoformat()
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise ExtractionError("服务端返回的 Token 有效期无效") from None
    return TokenBundle(
        email=email.strip(), **tokens, expires_in=expires_in,
        obtained_at=now.isoformat(), expires_at=expires_at,
        **{key: str(getattr(result, key, "") or "") for key in (
            "chatgpt_account_id", "chatgpt_user_id", "plan_type", "sub",
        )},
    )


async def extract_and_save(
    email: str, password: str, output: str | Path, *, overwrite: bool = False, **kwargs,
) -> TokenBundle:
    """Extract tokens and atomically save their complete JSON representation."""
    from .storage import check_output, save_tokens

    check_output(output, overwrite=overwrite)
    result = await extract_tokens(email, password, **kwargs)
    save_tokens(result, output, overwrite=overwrite)
    return result
