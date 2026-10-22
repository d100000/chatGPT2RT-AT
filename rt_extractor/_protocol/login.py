"""Existing-account OAuth login extracted from botcore.protocol_register.

No registration, SMS rental, mailbox integration, database, or console imports.
All redirects are handled manually; callbacks are validated before code exchange.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .http_client import AUTH_BASE, build_client, json_headers, nav_headers, request_with_retry
from .errors import ProtocolError, RateLimitedError, raise_for_rate_limit
from .pkce import new_device_id, new_pkce, random_state_nonce
from .profile import random_profile
from .sentinel import SentinelGenerator
from .totp import generate_totp, normalize_totp_secret, totp_seconds_remaining

CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_REDIRECT_URI = "http://localhost:1455/auth/callback"
CHATGPT_SCOPE = "openid profile email offline_access"
OtpFetcher = Callable[[str], Awaitable[str]]
TotpFetcher = Callable[[], Awaitable[str]]


class AdditionalVerificationRequired(ProtocolError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        messages = {
            "email": "此账号还需要邮箱验证码，请提供邮箱验证回调后重试",
            "phone": "此账号还需要手机验证或绑定手机号，本模块不处理该步骤",
            "totp": "此账号需要 Authenticator 2FA，请提供 TOTP 密钥或动态码回调",
        }
        super().__init__(messages.get(kind, "此账号需要额外验证"))


@dataclass(slots=True)
class ChatGPTLoginResult:
    email: str
    access_token: str = field(repr=False)
    id_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int = 0
    chatgpt_account_id: str = ""
    chatgpt_user_id: str = ""
    plan_type: str = ""
    sub: str = ""
    duration_seconds: float = 0.0
    device_id: str = ""
    proxy_used: str | None = field(default=None, repr=False)


def _resolve_url(value: str, base: str = AUTH_BASE + "/") -> str:
    """Allow only the issuer origin and the exact registered loopback callback."""
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError("认证响应缺少下一步地址")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value) or "\\" in value:
        raise ProtocolError("认证响应包含无效跳转地址")
    candidate = urljoin(base, value.strip())
    try:
        parsed = urlsplit(candidate)
        origin = (parsed.scheme, parsed.netloc)
        callback = urlsplit(CHATGPT_REDIRECT_URI)
        allowed_callback = (
            origin == (callback.scheme, callback.netloc)
            and parsed.path == callback.path
        )
        if parsed.fragment or parsed.username or parsed.password:
            raise ValueError
        if origin != ("https", "auth.openai.com") and not allowed_callback:
            raise ValueError
    except ValueError:
        raise ProtocolError("认证流程返回了不受信任的跳转地址") from None
    return candidate


def _callback_code(url: str, expected_state: str) -> str:
    candidate = _resolve_url(url)
    parsed = urlsplit(candidate)
    callback = urlsplit(CHATGPT_REDIRECT_URI)
    if (parsed.scheme, parsed.netloc, parsed.path) != (
        callback.scheme, callback.netloc, callback.path,
    ):
        return ""
    query = parse_qs(parsed.query, keep_blank_values=True)
    states = query.get("state", [])
    if len(states) != 1 or not expected_state or not hmac.compare_digest(
        states[0].encode("utf-8"), expected_state.encode("utf-8"),
    ):
        raise ProtocolError("OAuth 回调 state 校验失败")
    if "error" in query:
        raise ProtocolError("OAuth 授权被拒绝或已取消")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0].strip():
        raise ProtocolError("OAuth 回调缺少有效授权码")
    return codes[0]


def _body(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _page_type(payload: dict[str, Any]) -> str:
    page = payload.get("page")
    return str(page.get("type") or "") if isinstance(page, dict) else ""


def _next_url(payload: dict[str, Any], response: Any = None, *, base: str = AUTH_BASE + "/") -> str:
    location = response.headers.get("Location", "") if response is not None else ""
    if location:
        return _resolve_url(location, base)
    for source in (payload, payload.get("data"), payload.get("page")):
        if not isinstance(source, dict):
            continue
        for key in ("continue_url", "redirect_url", "redirectUrl", "continueUrl", "url"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return _resolve_url(value, base)
    return ""


def _check_status(response: Any, stage: str, *, redirects: bool = False) -> None:
    raise_for_rate_limit(response)
    allowed = (200, 201, 204, 301, 302, 303, 307, 308) if redirects else (200, 201, 204)
    if response.status_code not in allowed:
        raise ProtocolError(f"{stage}失败（HTTP {response.status_code}）")


def _cookie_payload(client: Any, name: str) -> dict[str, Any]:
    for cookie in client.cookies.jar:
        if cookie.name != name or cookie.domain.lstrip(".") != "auth.openai.com":
            continue
        try:
            value = cookie.value.split(".", 1)[0]
            payload = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
            if isinstance(payload, dict):
                return payload
        except (ValueError, TypeError, UnicodeError):
            continue
    return {}


def _workspace_id(data: dict[str, Any]) -> str:
    for source in (data, data.get("client_auth_session")):
        if not isinstance(source, dict):
            continue
        workspaces = source.get("workspaces")
        if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], dict):
            value = workspaces[0].get("id")
            if isinstance(value, str) and value:
                return value
    return ""


def _orgs(data: dict[str, Any]) -> list[dict[str, Any]]:
    for source in (data, data.get("data")):
        if isinstance(source, dict) and isinstance(source.get("orgs"), list):
            return [org for org in source["orgs"] if isinstance(org, dict)]
    return []


async def _navigate(
    client: Any, *, profile: Any, device_id: str, start_url: str,
    expected_state: str, params: dict[str, str] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    current = _resolve_url(start_url)
    headers = nav_headers(profile, device_id, site="same-origin")
    headers["Referer"] = AUTH_BASE + "/"
    for _ in range(16):
        code = _callback_code(current, expected_state)
        if code:
            return code, current, {}
        response = await request_with_retry(
            client, "GET", current, params=params, headers=headers,
            follow_redirects=False, retries=1,
        )
        params = None
        _check_status(response, "授权跳转", redirects=True)
        location = response.headers.get("Location", "")
        if response.status_code in (301, 302, 303, 307, 308):
            if not location:
                raise ProtocolError("认证跳转缺少 Location")
            current = _resolve_url(location, current)
            continue
        return "", current, _body(response)
    raise ProtocolError("认证跳转次数超过限制")


async def _post_json(
    client: Any, path: str, data: dict[str, Any], *, profile: Any,
    device_id: str, referer: str, stage: str,
    sentinel: SentinelGenerator | None = None, sentinel_flow: str = "",
) -> tuple[str, dict[str, Any]]:
    headers = json_headers(profile, device_id, referer=_resolve_url(referer))
    if sentinel is not None:
        result = await sentinel.sentinel_token(client, sentinel_flow)
        headers["openai-sentinel-token"] = result.token_json
        if result.so_token:
            headers["openai-sentinel-so-token"] = result.so_token
    response = await request_with_retry(
        client, "POST", AUTH_BASE + path,
        content=json.dumps(data), headers=headers, follow_redirects=False, retries=0,
    )
    _check_status(response, stage, redirects=True)
    body = _body(response)
    return _next_url(body, response, base=AUTH_BASE + path), body


async def _validate_mfa_totp(
    client: Any, *, profile: Any, device_id: str, challenge_url: str, code: str,
) -> tuple[str, dict[str, Any]]:
    target = _resolve_url(challenge_url)
    parsed = urlsplit(target)
    if parsed.netloc != "auth.openai.com" or not re.fullmatch(r"/mfa-challenge/[^/]+", parsed.path):
        raise ProtocolError("认证响应包含无效的 MFA challenge 地址")
    factor_id = parsed.path.rsplit("/", 1)[-1]
    return await _post_json(
        client, "/api/accounts/mfa/verify",
        {"id": factor_id, "type": "totp", "code": code},
        profile=profile, device_id=device_id, referer=target, stage="2FA 验证",
    )


async def _validate_email_otp(
    client: Any, *, profile: Any, device_id: str, code: str, sentinel: SentinelGenerator,
) -> tuple[str, dict[str, Any]]:
    headers = json_headers(profile, device_id, referer=AUTH_BASE + "/email-verification")
    url = AUTH_BASE + "/api/accounts/email-otp/validate"
    for attempt in range(2):
        if attempt:
            result = await sentinel.sentinel_token(client, "authorize_continue")
            headers["openai-sentinel-token"] = result.token_json
            if result.so_token:
                headers["openai-sentinel-so-token"] = result.so_token
        response = await request_with_retry(
            client, "POST", url, content=json.dumps({"code": code}),
            headers=headers, follow_redirects=False, retries=0,
        )
        if response.status_code in (200, 201, 204, 301, 302, 303, 307, 308):
            body = _body(response)
            return _next_url(body, response, base=url), body
        if response.status_code in (401, 403) and attempt == 0:
            continue
        _check_status(response, "邮箱验证码验证")
    raise ProtocolError("邮箱验证码验证失败")


async def _consent_next(
    client: Any, *, profile: Any, device_id: str, referer: str,
    pkce_challenge: str, state: str,
) -> tuple[str, dict[str, Any]]:
    workspace = _workspace_id(_cookie_payload(client, "oai-client-auth-session"))
    if not workspace:
        response = await request_with_retry(
            client, "GET", AUTH_BASE + "/api/accounts/client_auth_session_dump",
            headers=nav_headers(profile, device_id, site="same-origin"),
            follow_redirects=False, retries=1,
        )
        if response.status_code == 200:
            workspace = _workspace_id(_body(response))
    if workspace:
        return await _post_json(
            client, "/api/accounts/workspace/select", {"workspace_id": workspace},
            profile=profile, device_id=device_id, referer=referer, stage="工作区授权",
        )
    verifier = _cookie_payload(client, "login_session").get("login_challenge")
    params = {
        "client_id": CHATGPT_CLIENT_ID, "code_challenge_method": "S256",
        "code_challenge": pkce_challenge, "state": state,
        "codex_cli_simplified_flow": "true", "id_token_add_organizations": "true",
        "redirect_uri": CHATGPT_REDIRECT_URI, "response_type": "code", "scope": CHATGPT_SCOPE,
    }
    if isinstance(verifier, str) and verifier:
        params["login_verifier"] = verifier
    else:
        params["prompt"] = "login"
    return AUTH_BASE + "/api/oauth/oauth2/auth?" + urlencode(params), {}


async def _organization_next(
    client: Any, *, profile: Any, device_id: str, referer: str,
    organizations: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    org = next((o for o in organizations if str(o.get("kind") or "").lower() == "personal"), None)
    org = org or (organizations[0] if organizations else None)
    if not org or not isinstance(org.get("id"), str) or not org["id"]:
        raise ProtocolError("工作区未返回可选择的组织")
    project_id = ""
    projects = org.get("projects")
    if isinstance(projects, list) and projects and isinstance(projects[0], dict):
        project_id = projects[0].get("id") or ""
    project_id = project_id or org.get("default_project_id") or org.get("project_id") or ""
    data = {"org_id": org["id"]}
    if isinstance(project_id, str) and project_id:
        data["project_id"] = project_id
    return await _post_json(
        client, "/api/accounts/organization/select", data,
        profile=profile, device_id=device_id, referer=referer, stage="组织授权",
    )


def _jwt_claims(token: str) -> dict[str, Any]:
    """Decode display metadata only; this is not identity/signature verification."""
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError, UnicodeError):
        return {}


async def exchange_code_for_token(client: Any, *, code: str, code_verifier: str) -> dict[str, Any]:
    response = await request_with_retry(
        client, "POST", AUTH_BASE + "/oauth/token",
        content=urlencode({
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": CHATGPT_REDIRECT_URI, "client_id": CHATGPT_CLIENT_ID,
            "code_verifier": code_verifier,
        }),
        headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json"},
        follow_redirects=False, retries=0,
    )
    _check_status(response, "Token 兑换")
    payload = _body(response)
    for key in ("access_token", "refresh_token", "id_token"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ProtocolError(f"Token 兑换响应缺少有效的 {key}")
    return payload


def _validate_code(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6}", value.strip()):
        raise ProtocolError("验证码必须为 6 位数字")
    return value.strip()


async def login_get_tokens(
    *, email: str, password: str, totp_secret: str | None = None,
    totp_fetcher: TotpFetcher | None = None, otp_fetcher: OtpFetcher | None = None,
    proxy: str | None = None, on_step: Callable[[str, str], None] | None = None,
) -> ChatGPTLoginResult:
    """Authenticate an existing account and return all three OAuth tokens.

    A TOTP seed is generated locally. Manual codes are requested only after the
    server presents the corresponding challenge. Other verification is explicit.
    """
    if not isinstance(email, str) or "@" not in email.strip():
        raise ProtocolError("请输入有效的账号邮箱")
    if not isinstance(password, str) or not password:
        raise ProtocolError("账号密码不能为空")
    if totp_secret is not None and totp_fetcher is not None:
        raise ProtocolError("2FA 密钥和动态码回调只能选择一种")
    if totp_secret is not None:
        try:
            totp_secret = normalize_totp_secret(totp_secret)
        except ValueError as exc:
            raise ProtocolError(str(exc)) from None
    email = email.strip()
    started = time.monotonic()
    profile = random_profile()
    device_id = new_device_id()
    pkce = new_pkce()
    state, nonce = random_state_nonce()
    sentinel = SentinelGenerator(device_id=device_id, user_agent=profile.user_agent)

    def step(key: str, message: str) -> None:
        if on_step is not None:
            try:
                on_step(key, message)
            except Exception:
                pass

    try:
        async with build_client(profile=profile, proxy=proxy) as client:
            client.cookies.set("oai-did", device_id, domain="auth.openai.com", path="/")
            step("authorize", "打开 OAuth 授权页")
            params = {
                "issuer": AUTH_BASE, "client_id": CHATGPT_CLIENT_ID,
                "audience": "https://api.openai.com/v1", "redirect_uri": CHATGPT_REDIRECT_URI,
                "device_id": device_id, "screen_hint": "login", "login_hint": email,
                "scope": CHATGPT_SCOPE, "response_type": "code", "response_mode": "query",
                "state": state, "nonce": nonce, "code_challenge": pkce.challenge,
                "code_challenge_method": "S256", "prompt": "login", "max_age": "0",
            }
            code, current, payload = await _navigate(
                client, profile=profile, device_id=device_id,
                start_url=AUTH_BASE + "/api/accounts/authorize", expected_state=state, params=params,
            )
            verified_password = verified_totp = verified_email = consent_done = False
            organizations: list[dict[str, Any]] = []
            for _ in range(20):
                if code:
                    break
                code = _callback_code(current, state)
                if code:
                    break
                page = _page_type(payload)
                if page == "mfa_challenge":
                    current = _next_url(payload, base=current) or current
                path = urlsplit(current).path.rstrip("/")
                next_url = ""
                if page in ("add_phone", "phone_verification", "phone_otp") or path in (
                    "/add-phone", "/phone-verification", "/phone-otp",
                ):
                    raise AdditionalVerificationRequired("phone")
                if page == "mfa_challenge" or path.startswith("/mfa-challenge/"):
                    if verified_totp:
                        raise ProtocolError("2FA 验证后仍返回相同验证步骤")
                    if not totp_secret and totp_fetcher is None:
                        raise AdditionalVerificationRequired("totp")
                    step("totp", "验证 Authenticator 动态码")
                    if totp_secret:
                        remaining = totp_seconds_remaining()
                        if remaining <= 4:
                            await asyncio.sleep(remaining + 1)
                        totp_code = generate_totp(totp_secret)
                    else:
                        totp_code = _validate_code(await totp_fetcher())
                    next_url, payload = await _validate_mfa_totp(
                        client, profile=profile, device_id=device_id,
                        challenge_url=current, code=totp_code,
                    )
                    verified_totp = True
                elif page in ("email_otp_verification", "otp_verification") or path in (
                    "/email-verification", "/email-otp",
                ):
                    if verified_email:
                        raise ProtocolError("邮箱验证码验证后仍返回相同验证步骤")
                    if otp_fetcher is None:
                        raise AdditionalVerificationRequired("email")
                    step("email_otp", "等待并验证邮箱验证码")
                    otp = _validate_code(await otp_fetcher(email))
                    next_url, payload = await _validate_email_otp(
                        client, profile=profile, device_id=device_id, code=otp, sentinel=sentinel,
                    )
                    verified_email = True
                    if not next_url and not _page_type(payload):
                        next_url, payload = await _consent_next(
                            client, profile=profile, device_id=device_id,
                            referer=AUTH_BASE + "/email-verification",
                            pkce_challenge=pkce.challenge, state=state,
                        )
                        consent_done = True
                elif path in ("/log-in/password", "/log-in") or page in ("password", "login_password"):
                    if verified_password:
                        raise ProtocolError("密码验证后仍返回登录页")
                    step("password_verify", "验证账号密码")
                    next_url, payload = await _post_json(
                        client, "/api/accounts/password/verify", {"password": password},
                        profile=profile, device_id=device_id,
                        referer=AUTH_BASE + "/log-in/password", stage="密码验证",
                        sentinel=sentinel, sentinel_flow="password_verify",
                    )
                    verified_password = True
                elif "/codex/organization" in path:
                    step("organization", "完成组织授权")
                    next_url, payload = await _organization_next(
                        client, profile=profile, device_id=device_id,
                        referer=current, organizations=organizations,
                    )
                elif "/sign-in-with-chatgpt/" in path:
                    if consent_done:
                        raise ProtocolError("授权确认后仍停留在确认页面")
                    step("consent", "完成工作区授权")
                    next_url, payload = await _consent_next(
                        client, profile=profile, device_id=device_id, referer=current,
                        pkce_challenge=pkce.challenge, state=state,
                    )
                    consent_done = True
                else:
                    next_url = _next_url(payload, base=current)
                    if not next_url:
                        raise ProtocolError("登录遇到暂不支持的认证步骤")
                organizations = _orgs(payload) or organizations
                if not next_url:
                    page = _page_type(payload)
                    if page in ("add_phone", "phone_verification", "phone_otp"):
                        raise AdditionalVerificationRequired("phone")
                    if page in ("email_otp_verification", "otp_verification"):
                        next_url = AUTH_BASE + "/email-verification"
                    else:
                        raise ProtocolError("认证响应缺少下一步地址")
                current = _resolve_url(next_url, current)
                code = _callback_code(current, state)
                if code:
                    break
                # Challenges are consumed directly. Consent and organization pages
                # are loaded once before their form submissions, preserving cookies.
                next_path = urlsplit(current).path.rstrip("/")
                page = _page_type(payload)
                is_challenge = page in (
                    "mfa_challenge", "email_otp_verification", "otp_verification",
                    "add_phone", "phone_verification", "phone_otp", "password", "login_password",
                ) or next_path in (
                    "/log-in/password", "/log-in", "/email-verification", "/email-otp",
                    "/add-phone", "/phone-verification", "/phone-otp",
                ) or next_path.startswith("/mfa-challenge/")
                if not is_challenge:
                    code, current, page_payload = await _navigate(
                        client, profile=profile, device_id=device_id,
                        start_url=current, expected_state=state,
                    )
                    payload = page_payload
                    organizations = _orgs(payload) or organizations
            if not code:
                raise ProtocolError("认证步骤超过限制，未获得授权码")
            step("token_exchange", "兑换 AT、RT 和 ID Token")
            tokens = await exchange_code_for_token(client, code=code, code_verifier=pkce.verifier)
            claims = _jwt_claims(tokens["access_token"])
            auth_info = claims.get("https://api.openai.com/auth")
            auth_info = auth_info if isinstance(auth_info, dict) else {}
            expires = tokens.get("expires_in", 0)
            if isinstance(expires, bool) or not isinstance(expires, (str, int, float)):
                raise ProtocolError("Token 响应有效期格式无效")
            try:
                expires_in = int(expires)
                if expires_in < 0:
                    raise ValueError
            except (ValueError, OverflowError):
                raise ProtocolError("Token 响应有效期格式无效") from None
            return ChatGPTLoginResult(
                email=email, access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"], id_token=tokens["id_token"],
                expires_in=expires_in,
                chatgpt_account_id=str(auth_info.get("chatgpt_account_id") or ""),
                chatgpt_user_id=str(auth_info.get("chatgpt_user_id") or ""),
                plan_type=str(auth_info.get("chatgpt_plan_type") or ""),
                sub=str(claims.get("sub") or ""),
                duration_seconds=time.monotonic() - started, device_id=device_id, proxy_used=proxy,
            )
    except ProtocolError:
        raise
    except Exception:
        raise ProtocolError("登录请求失败，请检查网络连接或认证服务响应") from None
