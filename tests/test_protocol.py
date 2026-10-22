"""Offline protocol checks: synthetic accounts and in-memory HTTP responses only."""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx

from rt_extractor._protocol import http_client, login
from rt_extractor._protocol.profile import random_profile
from rt_extractor._protocol.sentinel import SentinelResult
from rt_extractor._protocol.totp import generate_totp, normalize_totp_secret


AUTH = "https://auth.openai.com"
CALLBACK = "http://localhost:1455/auth/callback"
EMAIL = "synthetic@example.com"
PASSWORD = "synthetic-password-do-not-log"
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def _jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"synthetic.{encoded}.signature"


class TotpTests(unittest.TestCase):
    def test_rfc6238_sha1_vector(self):
        self.assertEqual(generate_totp(SECRET, 59), "287082")

    def test_supported_uri(self):
        self.assertEqual(
            normalize_totp_secret(f"otpauth://totp/Test?secret={SECRET}&algorithm=SHA1&digits=6&period=30"),
            SECRET,
        )

    def test_unsupported_or_ambiguous_uri_rejected(self):
        cases = [
            f"otpauth://hotp/Test?secret={SECRET}",
            f"otpauth://totp/Test?secret={SECRET}&algorithm=SHA256",
            f"otpauth://totp/Test?secret={SECRET}&digits=8",
            f"otpauth://totp/Test?secret={SECRET}&period=60",
            f"otpauth://totp/Test?secret={SECRET}&secret={SECRET}",
            "otpauth://totp/Test?secret=",
            "not-a-valid-secret-***",
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_totp_secret(value)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def test_curl_tls_verification_enabled(self):
        with patch.object(http_client, "_CurlAsyncSession") as session:
            http_client.CurlCffiClient(headers={}, proxy=None, timeout_s=20)
        self.assertIs(session.call_args.kwargs["verify"], True)

    async def test_curl_does_not_follow_redirects_implicitly(self):
        session = SimpleNamespace(
            request=AsyncMock(return_value=SimpleNamespace(status_code=302, text="", url=AUTH, headers={})),
        )
        with patch.object(http_client, "_CurlAsyncSession", return_value=session):
            client = http_client.CurlCffiClient(headers={}, proxy=None, timeout_s=20)
            await client.post(f"{AUTH}/api/accounts/password/verify", json={"password": PASSWORD})
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    async def test_httpx_does_not_follow_redirects_implicitly(self):
        with patch.object(http_client, "_CurlAsyncSession", None):
            client = http_client.build_client(profile=random_profile(), proxy=None)
            try:
                self.assertFalse(client.follow_redirects)
            finally:
                await client.aclose()


class Scenario:
    """An in-memory identity provider; no request can leave the MockTransport."""

    def __init__(self):
        self.requests = []
        self.state = ""
        self.authorize_target = "/log-in/password"
        self.password_result = {
            "page": {"type": "mfa_challenge"},
            "continue_url": "/mfa-challenge/synthetic-factor",
        }
        self.mfa_result = {"continue_url": "/sign-in-with-chatgpt/codex/consent"}
        self.email_result = {"continue_url": "/sign-in-with-chatgpt/codex/consent"}
        self.overrides = {}
        self.tokens = {
            "access_token": _jwt({
                "sub": "synthetic-subject",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "synthetic-account",
                    "chatgpt_user_id": "synthetic-user",
                    "chatgpt_plan_type": "free",
                },
            }),
            "refresh_token": "synthetic-rt-do-not-log",
            "id_token": "synthetic-id-token-do-not-log",
            "expires_in": 3600,
        }

    @property
    def callback(self):
        return f"{CALLBACK}?code=synthetic-code&state={self.state}"

    def __call__(self, request):
        self.requests.append(request)
        if str(request.url).startswith("http://localhost"):
            raise AssertionError("Callback code must be consumed without requesting localhost")
        if request.url.host != "auth.openai.com" or request.url.scheme != "https":
            raise AssertionError("Protocol attempted to request an untrusted origin")
        key = (request.method, request.url.path)
        if key in self.overrides:
            return self.overrides[key](request)
        if key == ("GET", "/api/accounts/authorize"):
            self.state = request.url.params["state"]
            target = self.authorize_target
            if callable(target):
                target = target(self)
            return httpx.Response(302, headers={"Location": target})
        if request.method == "GET" and request.url.path in {
            "/log-in/password", "/email-verification",
            "/mfa-challenge/synthetic-factor", "/sign-in-with-chatgpt/codex/organization",
        }:
            return httpx.Response(200, text="synthetic login page")
        if key == ("POST", "/api/accounts/password/verify"):
            return httpx.Response(200, json=self.password_result)
        if key == ("POST", "/api/accounts/mfa/verify"):
            return httpx.Response(200, json=self.mfa_result)
        if key == ("POST", "/api/accounts/email-otp/validate"):
            return httpx.Response(200, json=self.email_result)
        if key == ("GET", "/sign-in-with-chatgpt/codex/consent"):
            payload = base64.urlsafe_b64encode(json.dumps({"workspaces": [{"id": "synthetic-workspace"}]}).encode()).decode().rstrip("=")
            return httpx.Response(200, text="synthetic consent page", headers={
                "Set-Cookie": f"oai-client-auth-session={payload}.timestamp.signature; Domain=auth.openai.com; Path=/; Secure",
            })
        if key == ("POST", "/api/accounts/workspace/select"):
            return httpx.Response(200, json={
                "continue_url": "/sign-in-with-chatgpt/codex/organization",
                "data": {"orgs": [{"id": "synthetic-org", "projects": [{"id": "synthetic-project"}]}]},
            })
        if key == ("POST", "/api/accounts/organization/select"):
            return httpx.Response(200, json={"continue_url": self.callback})
        if key == ("POST", "/oauth/token"):
            return httpx.Response(200, json=self.tokens)
        raise AssertionError(f"Unexpected synthetic request: {key}")

    def body(self, path):
        request = next(r for r in self.requests if r.url.path == path and r.method == "POST")
        return json.loads(request.content)

    @property
    def paths(self):
        return [r.url.path for r in self.requests]


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def run_scenario(self, scenario, **kwargs):
        client = httpx.AsyncClient(transport=httpx.MockTransport(scenario), follow_redirects=False)
        with (
            patch.object(login, "build_client", return_value=client),
            patch.object(login.SentinelGenerator, "sentinel_token", new=AsyncMock(return_value=SentinelResult("synthetic-sentinel"))),
            patch("rt_extractor._protocol.totp.time.time", return_value=59),
        ):
            return await login.login_get_tokens(email=EMAIL, password=PASSWORD, **kwargs)

    async def test_password_totp_consent_and_all_tokens(self):
        scenario = Scenario()
        result = await self.run_scenario(scenario, totp_secret=SECRET)
        self.assertEqual(result.access_token, scenario.tokens["access_token"])
        self.assertEqual(result.refresh_token, scenario.tokens["refresh_token"])
        self.assertEqual(result.id_token, scenario.tokens["id_token"])
        self.assertEqual(result.expires_in, 3600)
        self.assertEqual(result.chatgpt_account_id, "synthetic-account")
        self.assertEqual(result.plan_type, "free")
        self.assertEqual(scenario.body("/api/accounts/password/verify")["password"], PASSWORD)
        self.assertEqual(scenario.body("/api/accounts/mfa/verify"), {
            "id": "synthetic-factor", "type": "totp", "code": "287082",
        })
        self.assertEqual(scenario.body("/api/accounts/workspace/select")["workspace_id"], "synthetic-workspace")
        exchange = next(r for r in scenario.requests if r.url.path == "/oauth/token")
        form = parse_qs(exchange.content.decode())
        self.assertEqual(form["grant_type"], ["authorization_code"])
        self.assertEqual(form["code"], ["synthetic-code"])
        self.assertEqual(form["redirect_uri"], [CALLBACK])
        self.assertGreaterEqual(len(form["code_verifier"][0]), 43)
        verifier = form["code_verifier"][0]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(scenario.requests[0].url.params["code_challenge"], challenge)
        self.assertEqual(scenario.requests[0].url.params["code_challenge_method"], "S256")

    async def test_dynamic_code_callback(self):
        scenario = Scenario()
        fetcher = AsyncMock(return_value="654321")
        await self.run_scenario(scenario, totp_fetcher=fetcher)
        fetcher.assert_awaited_once_with()
        self.assertEqual(scenario.body("/api/accounts/mfa/verify")["code"], "654321")

    async def test_direct_callback_is_consumed_without_localhost_request(self):
        scenario = Scenario()
        scenario.authorize_target = lambda current: current.callback
        await self.run_scenario(scenario)
        self.assertEqual(scenario.paths, ["/api/accounts/authorize", "/oauth/token"])

    async def test_untrusted_redirect_or_mismatched_state_rejected(self):
        targets = [
            lambda s: "https://attacker.example/auth/callback?code=synthetic-code&state=" + s.state,
            lambda s: "http://localhost:1455/auth/callback.evil?code=synthetic-code&state=" + s.state,
            lambda s: "http://localhost:1455@attacker.example/auth/callback?code=synthetic-code&state=" + s.state,
            lambda s: "http://localhost:1455/auth/callback?code=synthetic-code&state=wrong",
            lambda s: "http://localhost:1455/auth/callback?code=synthetic-code",
            lambda s: f"{CALLBACK}?code=synthetic-code&state={s.state}&state={s.state}",
            lambda s: f"{CALLBACK}?code=first&code=second&state={s.state}",
            lambda s: f"{CALLBACK}?error=access_denied&state={s.state}",
        ]
        for target in targets:
            with self.subTest(target=target):
                scenario = Scenario()
                scenario.authorize_target = target
                with self.assertRaises(login.ProtocolError):
                    await self.run_scenario(scenario)
                self.assertEqual(scenario.paths, ["/api/accounts/authorize"])

    async def test_extra_email_verification_uses_callback(self):
        scenario = Scenario()
        scenario.password_result = {"page": {"type": "email_otp_verification"}, "continue_url": "/email-verification"}
        fetcher = AsyncMock(return_value="246810")
        await self.run_scenario(scenario, otp_fetcher=fetcher)
        fetcher.assert_awaited_once_with(EMAIL)
        self.assertEqual(scenario.body("/api/accounts/email-otp/validate")["code"], "246810")

    async def test_authorize_email_then_totp_then_consent(self):
        scenario = Scenario()
        scenario.authorize_target = "/email-verification"
        scenario.email_result = {
            "page": {"type": "mfa_challenge"},
            "continue_url": "/mfa-challenge/synthetic-factor",
        }
        email_fetcher = AsyncMock(return_value="246810")
        totp_fetcher = AsyncMock(return_value="654321")
        result = await self.run_scenario(scenario, otp_fetcher=email_fetcher, totp_fetcher=totp_fetcher)
        self.assertEqual(result.refresh_token, scenario.tokens["refresh_token"])
        self.assertNotIn("/api/accounts/password/verify", scenario.paths)
        self.assertLess(
            scenario.paths.index("/api/accounts/email-otp/validate"),
            scenario.paths.index("/api/accounts/mfa/verify"),
        )
        email_fetcher.assert_awaited_once_with(EMAIL)
        totp_fetcher.assert_awaited_once_with()

    async def test_phone_requirement_after_totp_stops(self):
        scenario = Scenario()
        scenario.mfa_result = {"page": {"type": "add_phone"}, "continue_url": "/add-phone"}
        with self.assertRaises(login.AdditionalVerificationRequired) as error:
            await self.run_scenario(scenario, totp_fetcher=AsyncMock(return_value="654321"))
        self.assertEqual(error.exception.kind, "phone")
        self.assertNotIn("/oauth/token", scenario.paths)
        self.assertFalse(any("add-phone/send" in path or "phone-otp" in path for path in scenario.paths))

    async def test_external_mfa_continuation_cannot_receive_code(self):
        scenario = Scenario()
        scenario.password_result = {
            "page": {"type": "mfa_challenge"},
            "continue_url": "https://attacker.example/mfa-challenge/synthetic-factor",
        }
        fetcher = AsyncMock(return_value="654321")
        with self.assertRaises(login.ProtocolError):
            await self.run_scenario(scenario, totp_fetcher=fetcher)
        fetcher.assert_not_awaited()
        self.assertNotIn("/api/accounts/mfa/verify", scenario.paths)

    async def test_missing_additional_verification_stops_cleanly(self):
        for page, continuation, expected in [
            ("email_otp_verification", "/email-verification", "email"),
            ("add_phone", "/add-phone", "phone"),
            ("phone_verification", "/phone-verification", "phone"),
            ("mfa_challenge", "/mfa-challenge/synthetic-factor", "totp"),
        ]:
            with self.subTest(page=page):
                scenario = Scenario()
                scenario.password_result = {"page": {"type": page}, "continue_url": continuation}
                with self.assertRaises(login.AdditionalVerificationRequired) as error:
                    await self.run_scenario(scenario)
                self.assertEqual(error.exception.kind, expected)
                self.assertNotIn("/oauth/token", scenario.paths)
                self.assertFalse(any("add-phone/send" in path or "phone-otp" in path for path in scenario.paths))

    async def test_every_token_required_and_failure_redacted(self):
        for field in ("access_token", "refresh_token", "id_token"):
            with self.subTest(field=field):
                scenario = Scenario()
                scenario.authorize_target = lambda current: current.callback
                scenario.tokens.pop(field)
                with self.assertRaises(login.ProtocolError) as error:
                    await self.run_scenario(scenario)
                self.assertIn(field, str(error.exception))
                for secret in scenario.tokens.values():
                    if isinstance(secret, str):
                        self.assertNotIn(secret, str(error.exception))

    async def test_http_error_does_not_include_sensitive_response(self):
        scenario = Scenario()
        scenario.overrides[("POST", "/api/accounts/password/verify")] = lambda r: httpx.Response(
            403, json={"password": PASSWORD, "token": "synthetic-sensitive-response"},
        )
        with self.assertRaises(login.ProtocolError) as error:
            await self.run_scenario(scenario, totp_secret=SECRET)
        self.assertNotIn(PASSWORD, str(error.exception))
        self.assertNotIn("synthetic-sensitive-response", str(error.exception))
        self.assertIn("403", str(error.exception))


if __name__ == "__main__":
    unittest.main()
