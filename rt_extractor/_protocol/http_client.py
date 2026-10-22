"""HTTP adapters and request headers for the isolated OAuth login flow."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Optional

import httpx

from .profile import Profile

logger = logging.getLogger(__name__)

try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
except ImportError:
    _CurlAsyncSession = None

AUTH_BASE = "https://auth.openai.com"

def _hex16() -> str:
    return secrets.token_bytes(8).hex()


def _hex32_dashes_removed() -> str:
    return secrets.token_bytes(16).hex()


def make_trace_headers() -> dict[str, str]:
    """W3C traceparent + Datadog 追踪头。OpenAI 前端 SPA 真实在带。"""
    trace_id = _hex16()
    parent_id = _hex16()
    return {
        "traceparent": f"00-{_hex32_dashes_removed()}-{parent_id}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def json_headers(profile: Profile, device_id: str, referer: str) -> dict[str, str]:
    """application/json POST 用的标准头（不含 sentinel）。

    对齐 wuzu-image common_headers：完整 sec-ch-ua-* 指纹头集合。
    """
    h = {
        "accept": "application/json",
        "content-type": "application/json",
        "accept-language": profile.locale,
        "origin": AUTH_BASE,
        "priority": "u=1, i",
        "referer": referer,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "oai-device-id": device_id,
        "sec-ch-ua": profile.sec_ch_ua,
        "sec-ch-ua-arch": '"x86_64"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version-list": profile.sec_ch_ua_full_version_list,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": profile.sec_ch_ua_platform,
        "sec-ch-ua-platform-version": '"10.0.0"',
        "user-agent": profile.user_agent,
    }
    h.update(make_trace_headers())
    return h


def nav_headers(profile: Profile, device_id: str, *, site: str = "same-origin") -> dict[str, str]:
    """整页跳转类 GET 用的头集合（authorize / email-otp/send / consent 链）。

    对齐 wuzu-image navigate_headers：完整 sec-ch-ua-* 指纹头集合。
    """
    return {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "accept-language": profile.locale,
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": site,
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "oai-device-id": device_id,
        "sec-ch-ua": profile.sec_ch_ua,
        "sec-ch-ua-arch": '"x86_64"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version-list": profile.sec_ch_ua_full_version_list,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": profile.sec_ch_ua_platform,
        "sec-ch-ua-platform-version": '"10.0.0"',
        "user-agent": profile.user_agent,
    }


class _CurlResponse:
    """Expose the response attributes used by the protocol flow."""

    def __init__(self, response: Any) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return int(self._response.status_code)

    @property
    def text(self) -> str:
        return str(self._response.text or "")

    @property
    def url(self) -> str:
        return str(self._response.url or "")

    @property
    def headers(self) -> Any:
        return self._response.headers

    def json(self) -> Any:
        return self._response.json()


class _CurlCookies:
    def __init__(self, session: Any) -> None:
        self._session = session

    @property
    def jar(self) -> Any:
        return self._session.cookies.jar

    def set(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        self._session.cookies.set(name, value, domain=domain, path=path)

    def get(self, name: str, default: str = "", *, domain: str = "") -> str:
        value = self._session.cookies.get(name, domain=domain)
        return str(value) if value is not None else default

    def delete(self, name: str, *, domain: str = "", path: str = "/") -> None:
        try:
            self._session.cookies.delete(name, domain=domain, path=path)
        except Exception:  # noqa: BLE001
            pass


class CurlCffiClient:
    """Small httpx-compatible wrapper with a Chrome TLS fingerprint."""

    def __init__(
        self,
        *,
        headers: dict[str, str],
        proxy: Optional[str],
        timeout_s: float,
    ) -> None:
        if _CurlAsyncSession is None:
            raise RuntimeError("curl_cffi 未安装")
        kwargs: dict[str, Any] = {
            "impersonate": "chrome",
            "verify": True,
            "trust_env": False,
            "headers": headers,
            "timeout": timeout_s,
        }
        if proxy:
            kwargs["proxy"] = proxy
        self._session = _CurlAsyncSession(**kwargs)

    @property
    def cookies(self) -> _CurlCookies:
        return _CurlCookies(self._session)

    async def request(self, method: str, url: str, **kwargs: Any) -> _CurlResponse:
        if "follow_redirects" in kwargs:
            kwargs["allow_redirects"] = kwargs.pop("follow_redirects")
        if "content" in kwargs and "data" not in kwargs:
            kwargs["data"] = kwargs.pop("content")
        kwargs.setdefault("allow_redirects", False)
        response = await self._session.request(method.upper(), url, **kwargs)
        return _CurlResponse(response)

    async def get(self, url: str, **kwargs: Any) -> _CurlResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> _CurlResponse:
        return await self.request("POST", url, **kwargs)

    async def aclose(self) -> None:
        await self._session.close()

    async def __aenter__(self) -> "CurlCffiClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()


def build_client(
    *,
    profile: Profile,
    proxy: Optional[str],
    timeout_s: float = 60.0,
) -> Any:
    """构建独立账号登录使用的 AsyncClient。

    - cookie jar 自动管理
    - proxy 单一 URL（http/https/socks 任意）
    - 禁止自动跟随跳转，由登录状态机逐跳校验 origin 和 OAuth state
    - 一份 UA / Accept-Language 全程一致
    """
    headers = {
        "user-agent": profile.user_agent,
        "accept-language": profile.locale,
        "sec-ch-ua": profile.sec_ch_ua,
        "sec-ch-ua-arch": '"x86_64"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version-list": profile.sec_ch_ua_full_version_list,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": profile.sec_ch_ua_platform,
        "sec-ch-ua-platform-version": '"10.0.0"',
    }
    is_socks = bool(proxy and proxy.lower().startswith("socks"))
    if _CurlAsyncSession is not None and not is_socks:
        logger.info("使用 curl_cffi Chrome TLS 指纹")
        return CurlCffiClient(
            headers=headers,
            proxy=proxy,
            timeout_s=timeout_s,
        )

    # SOCKS5 → SOCKS5H：DNS 走代理端解析，减少本地 DNS 泄露
    effective_proxy = proxy
    if proxy and proxy.startswith("socks5://"):
        effective_proxy = "socks5h://" + proxy[len("socks5://"):]
    # transport 级别重试 —— 处理 ConnectError / RemoteProtocolError /
    # "Server disconnected without sending a response"（代理 keep-alive 断后重连）。
    transport_kwargs: dict[str, Any] = {"retries": 2}
    if effective_proxy:
        transport_kwargs["proxy"] = effective_proxy
    transport = httpx.AsyncHTTPTransport(**transport_kwargs)

    kwargs: dict[str, Any] = {
        "headers": headers,
        "timeout": httpx.Timeout(timeout_s, connect=20.0),
        "follow_redirects": False,
        "http2": False,
        "transport": transport,
        "trust_env": False,
    }
    return httpx.AsyncClient(**kwargs)


async def request_with_retry(
    client: Any,
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff_s: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """httpx 请求 + 应用层重试。

    捕获 RemoteProtocolError / ReadError / ConnectError 共 N 次。
    OpenAI / 代理 keep-alive 在 IMAP 长等候后断开是常见的，必须能恢复。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            return await client.request(method, url, **kwargs)
        except Exception as exc:  # httpx and curl_cffi expose different network errors
            last_exc = exc
            message = str(exc).lower()
            if "unable to connect to proxy" in message or "proxyerror" in message:
                break
            if attempt >= retries:
                break
            await asyncio.sleep(backoff_s * (attempt + 1))
    assert last_exc is not None
    raise last_exc

