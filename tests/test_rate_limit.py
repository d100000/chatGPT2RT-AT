"""Offline rate-limit classification; responses and requests are synthetic."""

from __future__ import annotations

from email.utils import formatdate
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from rt_extractor._protocol import errors, login
from rt_extractor._protocol.errors import ProtocolError, RateLimitedError, raise_for_rate_limit
from rt_extractor._protocol.sentinel import SentinelGenerator


class RateLimitTests(unittest.TestCase):
    def test_retry_after_numeric_date_default_and_bounds(self):
        now = 1_700_000_000.25
        cases = [
            (None, 30), ("", 30), ("invalid-date", 30),
            ("NaN", 30), ("Infinity", 30), ("-Infinity", 30),
            ("1.2", 2), ("0", 1), ("-5", 1), ("900.1", 900),
            ("99999999", 900),
            (formatdate(1_700_000_065, usegmt=True), 65),
            (formatdate(1_699_999_970, usegmt=True), 1),
        ]
        with patch.object(errors.time, "time", return_value=now):
            for header, expected in cases:
                with self.subTest(header=header):
                    headers = {} if header is None else {"Retry-After": header}
                    with self.assertRaises(RateLimitedError) as raised:
                        raise_for_rate_limit(httpx.Response(429, headers=headers))
                    self.assertEqual(raised.exception.retry_after, expected)
        self.assertEqual(RateLimitedError().retry_after, 30)
        self.assertIsNone(raise_for_rate_limit(httpx.Response(200)))

    def test_auth_status_preserves_typed_rate_limit_without_response_body(self):
        self.assertIs(login.ProtocolError, ProtocolError)
        self.assertIs(login.RateLimitedError, RateLimitedError)
        response = httpx.Response(
            429, headers={"Retry-After": "7"},
            json={"access_token": "synthetic-secret-must-not-appear-in-errors"},
        )
        with self.assertRaises(RateLimitedError) as raised:
            login._check_status(response, "合成认证")
        self.assertEqual(raised.exception.retry_after, 7)
        self.assertIsInstance(raised.exception, ProtocolError)
        self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertIsNone(login._check_status(httpx.Response(200), "合成认证"))
        with self.assertRaises(ProtocolError) as forbidden:
            login._check_status(httpx.Response(403), "合成认证")
        self.assertNotIsInstance(forbidden.exception, RateLimitedError)


class SentinelRateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_sentinel_propagates_rate_limit_without_retry_or_fallback(self):
        generator = SentinelGenerator(device_id="synthetic-device", user_agent="synthetic-agent")
        client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(
            429, headers={"Retry-After": "11"}, text="synthetic-response-body",
        )))
        with patch.object(SentinelGenerator, "_fallback_result") as fallback:
            with self.assertRaises(RateLimitedError) as raised:
                await generator.sentinel_token(client, "synthetic-flow")
        self.assertEqual(raised.exception.retry_after, 11)
        client.post.assert_awaited_once()
        fallback.assert_not_called()
        self.assertNotIn("synthetic-response-body", str(raised.exception))

        # A rate limit encountered during the existing transient retry must also
        # escape instead of being converted to the fallback Sentinel token.
        with patch.object(SentinelGenerator, "_build_sentinel", new=AsyncMock(side_effect=[
            RuntimeError("synthetic transient failure"), RateLimitedError(retry_after=9),
        ])) as build, patch.object(SentinelGenerator, "_fallback_result") as fallback:
            with self.assertRaises(RateLimitedError) as raised:
                await generator.sentinel_token(client, "synthetic-flow")
        self.assertEqual(raised.exception.retry_after, 9)
        self.assertEqual(build.await_count, 2)
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
