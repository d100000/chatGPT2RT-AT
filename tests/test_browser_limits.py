"""Offline contracts for signed browser identities and layered abuse budgets."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.cookies import SimpleCookie
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rt_extractor.abuse import AbuseGuard, AbuseLimited, Quota, RequestIdentity
from rt_extractor.browser_identity import (
    BrowserIdentities, DEVICE_COOKIE, DEVICE_MAX_AGE, SESSION_COOKIE, SESSION_MAX_AGE,
)
from rt_extractor.web import create_app
from tests.test_abuse import Clock, policy


def layered_policy(layer, **changes):
    base = policy()
    return replace(base, **{layer: replace(getattr(base, layer), **changes)})


class LayeredLimiterTests(unittest.TestCase):
    def test_ip_budget_is_shared_by_distinct_devices_and_sessions(self):
        guard = AbuseGuard(policy(requests=Quota(1, 60)), Clock())
        guard.check_request(guard.identity("203.0.113.1", "device-one", "session-one"))
        with self.assertRaises(AbuseLimited):
            guard.check_request(guard.identity("203.0.113.1", "device-two", "session-two"))
        guard.check_request(guard.identity("203.0.113.2", "device-two", "session-two"))

    def test_device_budget_survives_changed_ip_and_new_session(self):
        guard = AbuseGuard(layered_policy("device", requests=Quota(2, 60)), Clock())
        for number in (1, 2):
            guard.check_request(guard.identity(f"203.0.113.{number}", "device", f"session-{number}"))
        with self.assertRaises(AbuseLimited):
            guard.check_request(guard.identity("203.0.113.3", "device", "session-three"))
        guard.check_request(guard.identity("203.0.113.3", "other-device", "other-session"))

    def test_session_budget_survives_changed_ip(self):
        guard = AbuseGuard(layered_policy("session", requests=Quota(1, 60)), Clock())
        guard.check_request(guard.identity("203.0.113.1", "device", "session"))
        with self.assertRaises(AbuseLimited):
            guard.check_request(guard.identity("203.0.113.2", "device", "session"))
        guard.check_request(guard.identity("203.0.113.2", "device", "new-session"))

    def test_failed_dimension_does_not_charge_other_dimensions(self):
        base = policy(requests=Quota(1, 60))
        base = replace(base, device=replace(base.device, requests=Quota(1, 60)),
                       session=replace(base.session, requests=Quota(1, 60)))
        guard = AbuseGuard(base, Clock())
        guard.check_request(guard.identity("203.0.113.1", "device-one", "session-one"))
        with self.assertRaises(AbuseLimited):
            guard.check_request(guard.identity("203.0.113.2", "device-one", "session-two"))
        guard.check_request(guard.identity("203.0.113.2", "device-two", "session-two"))
        with self.assertRaises(AbuseLimited):
            guard.check_request(guard.identity("203.0.113.3", "device-three", "session-one"))
        guard.check_request(guard.identity("203.0.113.3", "device-three", "session-three"))

    def test_global_budget_remains_shared_by_all_dimensions(self):
        guard = AbuseGuard(policy(global_requests=Quota(2, 60)), Clock())
        for number in (1, 2):
            guard.check_request(guard.identity(f"203.0.113.{number}", f"device-{number}", f"session-{number}"))
        with self.assertRaises(AbuseLimited):
            guard.check_request(guard.identity("203.0.113.3", "device-three", "session-three"))

    def test_missing_identity_has_stable_peer_fallback_and_hmac_keys(self):
        guard = AbuseGuard(layered_policy("session", requests=Quota(1, 60)), Clock())
        anonymous = guard.identity("203.0.113.1")
        self.assertIsInstance(anonymous, RequestIdentity)
        self.assertEqual(anonymous, guard.identity("203.0.113.1"))
        self.assertNotEqual(anonymous.device, anonymous.session)
        self.assertNotEqual(anonymous.device, guard.identity("203.0.113.2").device)
        guard.check_request(anonymous)
        with self.assertRaises(AbuseLimited):
            guard.check_request(guard.identity("203.0.113.1"))
        known = guard.identity("203.0.113.2", "synthetic-device", "synthetic-session")
        guard.check_request(known)
        keys = repr(list(guard.limiter.windows))
        for raw in ("203.0.113.1", "203.0.113.2", "synthetic-device", "synthetic-session"):
            self.assertNotIn(raw, keys)

    def test_legacy_peer_strings_keep_the_ip_only_contract(self):
        base = layered_policy("device", requests=Quota(1, 60), account_work=Quota(1, 60))
        base = replace(base, session=replace(base.session, requests=Quota(1, 60), account_work=Quota(1, 60)))
        guard = AbuseGuard(base, Clock())
        for number in range(3):
            guard.check_request("legacy-peer")
            guard.reserve_accounts("legacy-peer", [f"synthetic-{number}@example.com"])

    def test_every_browser_budget_applies_to_both_client_dimensions(self):
        operations = {
            "requests": lambda guard, identity: guard.check_request(identity),
            "burst": lambda guard, identity: guard.check_request(identity),
            "static_requests": lambda guard, identity: guard.check_request(identity, static=True),
            "static_burst": lambda guard, identity: guard.check_request(identity, static=True),
            "control": lambda guard, identity: guard.check_request(identity, control=True),
            "submissions": lambda guard, identity: guard.check_action(identity, "submit"),
            "verification": lambda guard, identity: guard.check_action(identity, "verify"),
            "exports": lambda guard, identity: guard.check_action(identity, "export"),
            "access_failures": lambda guard, identity: guard.denied_access(identity),
        }
        for layer in ("device", "session"):
            for name, operation in operations.items():
                with self.subTest(layer=layer, budget=name):
                    guard = AbuseGuard(layered_policy(layer, **{name: Quota(1, 10)}), Clock())
                    first = guard.identity("203.0.113.1", "device", "session")
                    second = guard.identity("203.0.113.2", "device", "session")
                    operation(guard, first)
                    with self.assertRaises(AbuseLimited) as rejected:
                        operation(guard, second)
                    self.assertEqual(rejected.exception.retry_after, 10)

    def test_account_work_reservations_are_atomic_and_refund_all_layers(self):
        for layer in ("device", "session"):
            with self.subTest(layer=layer):
                guard = AbuseGuard(layered_policy(layer, account_work=Quota(1, 60)), Clock())
                first = guard.identity("203.0.113.1", "device", "session")
                reservation = guard.reserve_accounts(first, ["one@example.com"])
                second = guard.identity("203.0.113.2", "device", "session")
                with self.assertRaises(AbuseLimited):
                    guard.reserve_accounts(second, ["two@example.com"])
                reservation.release()
                reservation.release()
                guard.reserve_accounts(second, ["two@example.com"])
                with self.assertRaises(AbuseLimited):
                    guard.reserve_accounts(first, ["three@example.com"])

    def test_failed_weighted_account_batch_does_not_charge_any_account(self):
        base = layered_policy("device", account_work=Quota(1, 60))
        guard = AbuseGuard(replace(base, account_attempts=Quota(1, 60)), Clock())
        identity = guard.identity("203.0.113.1", "device", "session")
        with self.assertRaises(AbuseLimited):
            guard.reserve_accounts(identity, ["one@example.com", "two@example.com"])
        guard.reserve_accounts(identity, ["one@example.com"]).release()
        guard.reserve_accounts(identity, ["two@example.com"])

    def test_parallel_requests_never_exceed_device_or_session_budget(self):
        for layer in ("device", "session"):
            with self.subTest(layer=layer):
                guard = AbuseGuard(layered_policy(layer, requests=Quota(5, 60)), Clock())

                def attempt(number):
                    identity = guard.identity(f"203.0.113.{number + 1}", "device", "session")
                    try:
                        guard.check_request(identity)
                        return True
                    except AbuseLimited:
                        return False

                with ThreadPoolExecutor(max_workers=12) as executor:
                    self.assertEqual(sum(executor.map(attempt, range(50))), 5)

    def test_identity_issuance_uses_ip_and_global_budget(self):
        guard = AbuseGuard(policy(identity_issuance=Quota(1, 60), global_identity_issuance=Quota(2, 60)), Clock())
        guard.check_identity_issuance(guard.identity("203.0.113.1", "device-one", "session-one"))
        with self.assertRaises(AbuseLimited):
            guard.check_identity_issuance(guard.identity("203.0.113.1", "device-two", "session-two"))
        guard.check_identity_issuance(guard.identity("203.0.113.2"))
        with self.assertRaises(AbuseLimited):
            guard.check_identity_issuance(guard.identity("203.0.113.3"))

    def test_parallel_first_visits_cannot_overrun_identity_issuance_budget(self):
        guard = AbuseGuard(policy(identity_issuance=Quota(3, 60)), Clock())

        def attempt(number):
            identity = guard.identity("203.0.113.1", f"device-{number}", f"session-{number}")
            try:
                guard.check_identity_issuance(identity)
                return True
            except AbuseLimited:
                return False

        with ThreadPoolExecutor(max_workers=12) as executor:
            self.assertEqual(sum(executor.map(attempt, range(50))), 3)


class BrowserIdentityTests(unittest.TestCase):
    @staticmethod
    def jar(identity):
        return {cookie.name: cookie.value for cookie in identity.cookies}

    def test_empty_resolution_does_not_issue_but_html_mode_creates_both_ids(self):
        identities = BrowserIdentities()
        missing = identities.resolve({})
        self.assertIsNone(missing.device)
        self.assertIsNone(missing.session)
        self.assertEqual(missing.cookies, ())
        issued = identities.resolve({}, issue=True)
        self.assertRegex(issued.device, r"\A[0-9a-f]{64}\Z")
        self.assertRegex(issued.session, r"\A[0-9a-f]{64}\Z")
        self.assertNotEqual(issued.device, issued.session)
        self.assertEqual({cookie.name: cookie.max_age for cookie in issued.cookies},
                         {DEVICE_COOKIE: DEVICE_MAX_AGE, SESSION_COOKIE: None})

    def test_valid_cookies_are_retained_without_sliding_expiry(self):
        identities = BrowserIdentities()
        with patch("itsdangerous.timed.time.time", return_value=2_000_000_000):
            issued = identities.resolve({}, issue=True)
        cookies = self.jar(issued)
        with patch("itsdangerous.timed.time.time", return_value=2_000_000_000 + SESSION_MAX_AGE - 1):
            resolved = identities.resolve(cookies, issue=True)
        self.assertEqual((resolved.device, resolved.session), (issued.device, issued.session))
        self.assertEqual(resolved.cookies, ())
        with patch("itsdangerous.timed.time.time", return_value=2_000_000_000 + SESSION_MAX_AGE + 1):
            expired = identities.resolve(cookies)
        self.assertEqual(expired.device, issued.device)
        self.assertIsNone(expired.session)
        self.assertEqual(expired.cookies, ())

    def test_session_is_bound_to_device_and_only_missing_session_is_reissued(self):
        identities = BrowserIdentities()
        first = identities.resolve({}, issue=True)
        second = identities.resolve({}, issue=True)
        mixed = {DEVICE_COOKIE: self.jar(first)[DEVICE_COOKIE],
                 SESSION_COOKIE: self.jar(second)[SESSION_COOKIE]}
        invalid = identities.resolve(mixed)
        self.assertEqual(invalid.device, first.device)
        self.assertIsNone(invalid.session)
        repaired = identities.resolve(mixed, issue=True)
        self.assertEqual(repaired.device, first.device)
        self.assertNotIn(repaired.session, (first.session, second.session))
        self.assertEqual([cookie.name for cookie in repaired.cookies], [SESSION_COOKIE])
        without_device = identities.resolve({SESSION_COOKIE: self.jar(first)[SESSION_COOKIE]})
        self.assertIsNone(without_device.device)
        self.assertIsNone(without_device.session)

    def test_tampered_expired_future_and_previous_process_cookies_are_anonymous(self):
        identities = BrowserIdentities()
        with patch("itsdangerous.timed.time.time", return_value=2_000_000_000):
            issued = identities.resolve({}, issue=True)
        cookies = self.jar(issued)
        tampered = {name: value + "invalid" for name, value in cookies.items()}
        for name, resolver, values, now in (
            ("tampered", identities, tampered, 2_000_000_000),
            ("expired", identities, cookies, 2_000_000_000 + DEVICE_MAX_AGE + 1),
            ("future", identities, cookies, 1_999_999_999),
            ("restart", BrowserIdentities(), cookies, 2_000_000_000),
        ):
            with self.subTest(case=name), patch("itsdangerous.timed.time.time", return_value=now):
                invalid = resolver.resolve(values)
                self.assertIsNone(invalid.device)
                self.assertIsNone(invalid.session)
                self.assertEqual(invalid.cookies, ())


class BrowserWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.clock = Clock()

    def tearDown(self):
        self.directory.cleanup()

    def app(self, abuse_policy=None):
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("Browser security tests must not start extraction")

        app = create_app(self.output, extractor=forbidden,
                         abuse_policy=abuse_policy or policy(), abuse_clock=self.clock)
        app.config["TESTING"] = True
        return app, {"X-RT-CSRF": app.config["RT_CSRF_TOKEN"]}

    @staticmethod
    def response_cookies(response):
        cookies = SimpleCookie()
        for value in response.headers.getlist("Set-Cookie"):
            cookies.load(value)
        return cookies

    def assert_limited(self, response, retry=60):
        self.assertEqual(response.status_code, 429, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["retry_after"], retry)
        self.assertEqual(response.headers["Retry-After"], str(retry))
        self.assertFalse(response.headers.getlist("Set-Cookie"))

    def browser(self, app):
        client = app.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertIsNotNone(client.get_cookie(DEVICE_COOKIE))
        self.assertIsNotNone(client.get_cookie(SESSION_COOKIE))
        return client

    def test_only_successful_html_gets_issue_cookies_with_safe_attributes(self):
        app, headers = self.app()
        for path in ("/", "/converter", "/network"):
            for scheme in ("http", "https"):
                with self.subTest(path=path, scheme=scheme):
                    client = app.test_client()
                    response = client.get(path, base_url=f"{scheme}://localhost")
                    self.assertEqual(response.status_code, 200)
                    cookies = self.response_cookies(response)
                    self.assertEqual(set(cookies), {DEVICE_COOKIE, SESSION_COOKIE})
                    for cookie in cookies.values():
                        self.assertTrue(cookie["httponly"])
                        self.assertEqual(cookie["samesite"], "Lax")
                        self.assertEqual(cookie["path"], "/")
                        self.assertEqual(bool(cookie["secure"]), scheme == "https")
                    self.assertEqual(cookies[DEVICE_COOKIE]["max-age"], str(DEVICE_MAX_AGE))
                    self.assertEqual(cookies[SESSION_COOKIE]["max-age"], "")
                    self.assertEqual(cookies[SESSION_COOKIE]["expires"], "")
        cases = (("GET", "/health", {}), ("GET", "/static/app.css", {}),
                 ("GET", "/static/synthetic-missing.css", {}), ("GET", "/missing", {}),
                 ("HEAD", "/", {}), ("POST", "/", {}),
                 ("GET", "/api/jobs/current", headers),
                 ("GET", "/api/jobs/current", {}))
        for method, path, request_headers in cases:
            with self.subTest(method=method, path=path, headers=bool(request_headers)):
                with app.test_client().open(path, method=method, headers=request_headers) as response:
                    self.assertFalse(response.headers.getlist("Set-Cookie"))
        response = app.test_client().get("/", headers={"Origin": "https://invalid.example"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.headers.getlist("Set-Cookie"))

    def test_valid_browser_navigation_does_not_renew_cookies(self):
        app, _headers = self.app()
        client = self.browser(app)
        original = (client.get_cookie(DEVICE_COOKIE).value, client.get_cookie(SESSION_COOKIE).value)
        for path in ("/", "/converter", "/network"):
            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.headers.getlist("Set-Cookie"))
        self.assertEqual(original, (client.get_cookie(DEVICE_COOKIE).value, client.get_cookie(SESSION_COOKIE).value))

    def test_same_device_with_new_session_shares_device_action_budget(self):
        app, headers = self.app(layered_policy("device", exports=Quota(1, 60)))
        first = self.browser(app)
        second = app.test_client()
        second.set_cookie(DEVICE_COOKIE, first.get_cookie(DEVICE_COOKIE).value)
        response = second.get("/")
        self.assertEqual([*self.response_cookies(response)], [SESSION_COOKIE])
        self.assertNotEqual(first.get_cookie(SESSION_COOKIE).value, second.get_cookie(SESSION_COOKIE).value)
        self.assertEqual(first.get("/api/jobs/missing/rt", headers=headers).status_code, 404)
        self.assert_limited(second.get("/api/jobs/missing/rt", headers=headers))

    def test_distinct_sessions_on_same_device_have_independent_session_budgets(self):
        app, headers = self.app(layered_policy("session", exports=Quota(1, 60)))
        first = self.browser(app)
        second = app.test_client()
        second.set_cookie(DEVICE_COOKIE, first.get_cookie(DEVICE_COOKIE).value)
        self.assertEqual(second.get("/").status_code, 200)
        for client in (first, second):
            self.assertEqual(client.get("/api/jobs/missing/rt", headers=headers).status_code, 404)
            self.assert_limited(client.get("/api/jobs/missing/rt", headers=headers))

    def test_web_actions_enforce_each_browser_dimension(self):
        cases = (
            ("requests", "GET", "/health", {}, True, 200),
            ("burst", "GET", "/health", {}, True, 200),
            ("static_requests", "GET", "/static/app.css", {}, True, 200),
            ("static_burst", "GET", "/static/app.css", {}, True, 200),
            ("submissions", "POST", "/api/jobs", {}, True, 400),
            ("verification", "POST", "/api/jobs/missing/code", {"code": "123456", "challenge_id": "synthetic"}, True, 404),
            ("exports", "GET", "/api/jobs/missing/rt", {}, True, 404),
            ("control", "POST", "/api/jobs/missing/cancel", {}, True, 404),
            ("access_failures", "GET", "/api/jobs/current", {}, False, 403),
        )
        for layer in ("device", "session"):
            for budget, method, path, payload, csrf, expected in cases:
                with self.subTest(layer=layer, budget=budget):
                    app, headers = self.app(layered_policy(layer, **{budget: Quota(1, 60)}))
                    client = self.browser(app)
                    kwargs = {"headers": headers if csrf else {}}
                    if method == "POST":
                        kwargs["json"] = payload
                    with client.open(path, method=method, **kwargs) as response:
                        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
                    next_path = "/static/synthetic-missing.css" if budget.startswith("static_") else path
                    self.assert_limited(client.open(next_path, method=method, **kwargs))

    def test_failed_batch_start_refunds_browser_account_work_and_attempts(self):
        for layer in ("device", "session"):
            with self.subTest(layer=layer):
                base = layered_policy(layer, account_work=Quota(2, 60))
                app, headers = self.app(replace(base, account_attempts=Quota(1, 60)))
                client = self.browser(app)
                payload = {"accounts_text": "one@example.com----synthetic-password----JBSWY3DPEHPK3PXP\n"
                                           "two@example.com----synthetic-password----JBSWY3DPEHPK3PXP"}
                manager = app.extensions["rt_batches"]
                with patch.object(manager, "start", side_effect=OSError("synthetic failure")):
                    self.assertEqual(client.post("/api/batches", json=payload, headers=headers).status_code, 500)
                with patch.object(manager, "start", return_value={"id": "synthetic-batch"}) as start:
                    self.assertEqual(client.post("/api/batches", json=payload, headers=headers).status_code, 202)
                    extra = {"email": "three@example.com", "password": "synthetic-password"}
                    self.assert_limited(client.post("/api/jobs", json=extra, headers=headers))
                    self.assertEqual(start.call_count, 1)

    def test_cleared_forged_expired_and_header_ids_share_anonymous_fallback(self):
        for layer in ("device", "session"):
            for mode in ("cleared", "forged", "expired", "headers"):
                with self.subTest(layer=layer, mode=mode):
                    app, _headers = self.app(layered_policy(layer, requests=Quota(2, 60)))
                    with patch("itsdangerous.timed.time.time", return_value=2_000_000_000):
                        client = self.browser(app)
                    if mode != "expired":
                        for name in (DEVICE_COOKIE, SESSION_COOKIE):
                            client.delete_cookie(name)
                            if mode == "forged":
                                client.set_cookie(name, "attacker-generated-cookie")
                    now = 2_000_000_000 + (DEVICE_MAX_AGE + 1 if mode == "expired" else 0)
                    with patch("itsdangerous.timed.time.time", return_value=now):
                        for number, expected in ((1, 200), (2, 429), (3, 429)):
                            headers = {"X-Device-ID": f"device-{number}", "X-Session-ID": f"session-{number}",
                                       "X-Forwarded-For": f"203.0.113.{number}"} if mode == "headers" else {}
                            response = client.get("/health", headers=headers)
                            self.assertEqual(response.status_code, expected)
                            self.assertFalse(response.headers.getlist("Set-Cookie"))

    def test_identity_issuance_is_limited_without_penalizing_valid_navigation(self):
        app, _headers = self.app(policy(identity_issuance=Quota(2, 10)))
        first = self.browser(app)
        self.browser(app)
        third = app.test_client()
        self.assert_limited(third.get("/"), 10)
        response = first.get("/network")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.headers.getlist("Set-Cookie"))
        self.clock.advance(10)
        response = third.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(self.response_cookies(response)), {DEVICE_COOKIE, SESSION_COOKIE})

    def test_missing_session_costs_one_issuance_without_renewing_device(self):
        app, _headers = self.app(policy(identity_issuance=Quota(2, 60)))
        client = self.browser(app)
        device = client.get_cookie(DEVICE_COOKIE).value
        client.delete_cookie(SESSION_COOKIE)
        response = client.get("/converter")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(self.response_cookies(response)), {SESSION_COOKIE})
        self.assertEqual(client.get_cookie(DEVICE_COOKIE).value, device)
        client.delete_cookie(SESSION_COOKIE)
        self.assert_limited(client.get("/network"))


if __name__ == "__main__":
    unittest.main()
