"""Offline abuse-control contracts; credentials and network clients are synthetic."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, is_dataclass, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flask import Response

from rt_extractor.abuse import (
    AbuseGuard,
    AbuseLimited,
    AbusePolicy,
    LeasedIterator,
    Quota,
    WindowLimiter,
)
from rt_extractor.web import create_app
from tests.web_client import browser_headers, workspace_for
from rt_extractor.web_jobs import Job


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def policy(**changes):
    def relaxed(value):
        if isinstance(value, Quota):
            return Quota(100_000, 60)
        if is_dataclass(value):
            return replace(value, **{field.name: relaxed(getattr(value, field.name))
                                     for field in fields(value)})
        return value

    return replace(relaxed(AbusePolicy()), **changes)


class LimiterTests(unittest.TestCase):
    def test_rejected_retries_do_not_extend_the_original_window(self):
        clock = Clock()
        limiter = WindowLimiter(10, clock)
        charge = [("test", "peer", Quota(2, 10), 1)]
        limiter.reserve(charge)
        limiter.reserve(charge)
        for elapsed, expected in ((5, 5), (4.25, 1)):
            clock.advance(elapsed)
            with self.assertRaises(AbuseLimited) as rejected:
                limiter.reserve(charge)
            self.assertEqual(rejected.exception.retry_after, expected)
        clock.advance(.75)
        limiter.reserve(charge)

    def test_multi_rule_retry_uses_the_longest_block_without_partial_debits(self):
        clock = Clock()
        limiter = WindowLimiter(10, clock)
        limiter.reserve([("short", "peer", Quota(1, 10), 1),
                         ("long", "peer", Quota(1, 20), 1)])
        with self.assertRaises(AbuseLimited) as rejected:
            limiter.reserve([("new", "peer", Quota(1, 10), 1),
                             ("short", "peer", Quota(1, 10), 1),
                             ("long", "peer", Quota(1, 20), 1)])
        self.assertEqual(rejected.exception.retry_after, 20)
        limiter.reserve([("new", "peer", Quota(1, 10), 1)])

    def test_random_keys_cannot_evict_active_penalties_or_grow_the_expiry_heap(self):
        clock = Clock()
        limiter = WindowLimiter(2, clock)
        limiter.reserve([("test", "penalized", Quota(1, 100), 1)])
        limiter.reserve([("test", "other", Quota(1, 100), 1)])
        for number in range(100):
            with self.assertRaises(AbuseLimited):
                limiter.reserve([("test", f"random-{number}", Quota(1, 100), 1)])
        self.assertEqual(len(limiter.windows), 2)
        self.assertEqual(len(limiter._expiry), 2)
        with self.assertRaises(AbuseLimited):
            limiter.reserve([("test", "penalized", Quota(1, 100), 1)])
        clock.advance(100)
        limiter.reserve([("test", "new", Quota(1, 100), 1)])
        self.assertEqual(len(limiter.windows), 1)

    def test_refund_is_idempotent_and_cannot_refund_a_new_window(self):
        clock = Clock()
        limiter = WindowLimiter(1, clock)
        charge = [("test", "peer", Quota(1, 10), 1)]
        first = limiter.reserve(charge)
        first.release()
        first.release()
        second = limiter.reserve(charge)
        clock.advance(10)
        limiter.reserve(charge)
        second.release()
        with self.assertRaises(AbuseLimited):
            limiter.reserve(charge)
        self.assertEqual(len(limiter._expiry), 1)

    def test_parallel_admission_never_exceeds_the_quota(self):
        limiter = WindowLimiter(10, Clock())

        def attempt(_number):
            try:
                limiter.reserve([("test", "peer", Quota(5, 60), 1)])
                return True
            except AbuseLimited:
                return False

        with ThreadPoolExecutor(max_workers=12) as executor:
            self.assertEqual(sum(executor.map(attempt, range(50))), 5)

    def test_account_attempts_are_normalized_global_and_batch_atomic(self):
        guard = AbuseGuard(policy(account_attempts=Quota(1, 900)), Clock())
        guard.reserve_accounts("peer-one", [" Person@Example.com "])
        with self.assertRaises(AbuseLimited):
            guard.reserve_accounts("peer-two", ["person@example.COM", "other@example.com"])
        # The rejected mixed batch did not charge its otherwise valid account.
        guard.reserve_accounts("peer-two", ["other@example.com"])
        serialized_keys = repr(list(guard.limiter.windows))
        self.assertNotIn("person@", serialized_keys.lower())
        self.assertNotIn("other@", serialized_keys.lower())
        self.assertNotEqual(guard.peer("203.0.113.1"), guard.peer("203.0.113.2"))
        self.assertEqual(guard.peer("127.0.0.1"), guard.peer("::1"))

    def test_account_work_is_weighted_and_the_default_allows_a_full_batch(self):
        clock = Clock()
        guard = AbuseGuard(AbusePolicy(), clock)
        guard.reserve_accounts("peer", (f"first{i}@example.com" for i in range(1000)))
        guard.reserve_accounts("peer", (f"second{i}@example.com" for i in range(1000)))
        with self.assertRaises(AbuseLimited) as rejected:
            guard.reserve_accounts("peer", ["next@example.com"])
        self.assertEqual(rejected.exception.retry_after, 3600)

    def test_global_request_limit_is_shared_across_peers(self):
        guard = AbuseGuard(policy(global_requests=Quota(2, 60)), Clock())
        guard.check_request("one")
        guard.check_request("two")
        with self.assertRaises(AbuseLimited):
            guard.check_request("three")

    def test_iterator_releases_on_exhaustion_unstarted_close_and_stream_error(self):
        guard = AbuseGuard(policy(export_concurrency=1), Clock())
        lease = guard.acquire_export()
        stream = LeasedIterator([b"one", b"two"], lease)
        self.assertEqual(list(stream), [b"one", b"two"])
        stream.close()
        lease = guard.acquire_export()
        stream = LeasedIterator([b"unused"], lease)
        stream.close()

        def broken():
            yield b"first"
            raise RuntimeError("synthetic stream error")

        stream = LeasedIterator(broken(), guard.acquire_export())
        self.assertEqual(next(stream), b"first")
        with self.assertRaises(RuntimeError):
            next(stream)
        guard.acquire_export().release()
        self.assertEqual(guard._exports, 0)


class WebAbuseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.clock = Clock()

    def tearDown(self):
        self.directory.cleanup()

    def app(self, **changes):
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("A security test must not start real extraction")

        app = create_app(
            self.output,
            extractor=forbidden,
            abuse_policy=policy(**changes),
            abuse_clock=self.clock,
        )
        app.config["TESTING"] = True
        client = app.test_client()
        headers = browser_headers(app, client)
        app._fixture_client = client
        # Bootstrapping is a real request. Start each narrow quota test after
        # that window, while keeping the signed browser identity intact.
        self.clock.advance(3601)
        return app, client, headers

    def assert_limited(self, response, retry=None):
        self.assertEqual(response.status_code, 429, response.get_data(as_text=True))
        body = response.get_json()
        self.assertIsInstance(body["retry_after"], int)
        self.assertGreaterEqual(body["retry_after"], 1)
        self.assertEqual(response.headers["Retry-After"], str(body["retry_after"]))
        self.assertTrue(response.cache_control.no_store)
        if retry is not None:
            self.assertEqual(body["retry_after"], retry)
        for value in ("synthetic-password", "JBSWY3DPEHPK3PXP", "person@example.com"):
            self.assertNotIn(value, json.dumps(body))

    @staticmethod
    def credentials(email="person@example.com"):
        return {"email": email, "password": "synthetic-password", "totp_secret": "JBSWY3DPEHPK3PXP"}

    @classmethod
    def batch(cls, *emails):
        return {"accounts_text": "\n".join(f"{email}----synthetic-password----JBSWY3DPEHPK3PXP" for email in emails)}

    def terminal_job(self, app, status="success", size=0):
        job_id = "a" * 32
        raw = {
            "access_token": "synthetic-at",
            "refresh_token": "synthetic-rt",
            "id_token": "synthetic-id",
            "email": "person@example.com",
            "padding": "x" * size,
        }
        job = Job(
            id=job_id,
            email="person@example.com",
            status=status,
            result={"filename": f"tokens-{job_id}.json"},
            payload={"raw": raw, "cpa": {}, "sub2api": {}}
            if status == "success"
            else None,
        )
        workspace_for(app, app._fixture_client).jobs.jobs[job_id] = job
        return job

    def test_request_limit_ignores_spoofed_forwarding_headers_and_recovers(self):
        app, client, headers = self.app(requests=Quota(2, 10))
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/health", headers={"X-Forwarded-For": "203.0.113.1"}).status_code, 200)
        self.assert_limited(client.get("/health", headers={"Forwarded": "for=203.0.113.2"}), 10)
        self.clock.advance(9.5)
        self.assert_limited(client.get("/api/jobs/current", headers=headers), 1)
        self.clock.advance(.5)
        self.assertEqual(client.get("/api/jobs/current", headers=headers).status_code, 200)

    def test_global_limit_counts_distinct_direct_peers(self):
        app, client, _headers = self.app(global_requests=Quota(2, 10))
        for peer in ("203.0.113.1", "203.0.113.2"):
            self.assertEqual(client.get("/health", environ_overrides={"REMOTE_ADDR": peer}).status_code, 403)
        self.assert_limited(client.get("/health"), 10)

    def test_existing_and_missing_static_paths_share_a_fixed_budget(self):
        for field in ("static_requests", "global_static_requests", "static_burst", "global_static_burst"):
            with self.subTest(quota=field):
                _app, client, _headers = self.app(**{field: Quota(2, 10)})
                with client.get("/static/app.css") as response:
                    self.assertEqual(response.status_code, 200)
                self.assertEqual(client.get("/static/synthetic-missing-one.css").status_code, 404)
                for path in ("synthetic-missing-two.css", "synthetic-missing-three.css", "app.css"):
                    self.assert_limited(client.get(f"/static/{path}"), 10)
                self.clock.advance(10)
                with client.get("/static/app.css") as response:
                    self.assertEqual(response.status_code, 200)

    def test_static_and_dynamic_request_budgets_are_independent(self):
        quota_pairs = (("requests", "static_requests"),
                       ("global_requests", "global_static_requests"),
                       ("burst", "static_burst"),
                       ("global_burst", "global_static_burst"))
        for dynamic, static in quota_pairs:
            for first, second in (("/health", "/static/app.css"),
                                  ("/static/app.css", "/health")):
                with self.subTest(quota=dynamic, first=first):
                    _app, client, _headers = self.app(**{dynamic: Quota(1, 60), static: Quota(1, 60)})
                    with client.get(first) as response:
                        self.assertEqual(response.status_code, 200)
                    self.assert_limited(client.get(first), 60)
                    with client.get(second) as response:
                        self.assertEqual(response.status_code, 200)
                    self.assert_limited(client.get(second), 60)

    def test_cancel_has_a_separate_budget_when_general_requests_are_exhausted(self):
        app, client, headers = self.app(requests=Quota(1, 60), control=Quota(1, 60))
        job = self.terminal_job(app, status="error")
        self.assertEqual(client.get("/health").status_code, 200)
        self.assert_limited(client.get("/api/jobs/current", headers=headers))
        self.assertEqual(client.post(f"/api/jobs/{job.id}/cancel", json={}, headers=headers).status_code, 200)
        self.assert_limited(client.post(f"/api/jobs/{job.id}/cancel", json={}, headers=headers))

    def test_submission_routes_share_a_budget_while_status_remains_available(self):
        app, client, headers = self.app(submissions=Quota(1, 60))
        self.assertEqual(client.post("/api/jobs", json={}, headers=headers).status_code, 400)
        self.assert_limited(client.post("/api/batches", json={}, headers=headers), 60)
        self.assertEqual(client.get("/api/batches/current", headers=headers).status_code, 200)

    def test_access_failures_are_throttled_but_do_not_lock_out_a_valid_page_token(self):
        app, client, headers = self.app(access_failures=Quota(2, 300))
        for _ in range(2):
            self.assertEqual(client.get("/api/jobs/current").status_code, 403)
        self.assert_limited(client.get("/api/jobs/current"), 300)
        self.assertEqual(client.get("/api/jobs/current", headers=headers).status_code, 200)

    def test_account_budget_blocks_changed_case_and_mixed_batch_without_partial_charges(
        self,
    ):
        app, client, headers = self.app(account_attempts=Quota(1, 900))
        manager = workspace_for(app, client).jobs
        batches = workspace_for(app, client).batches
        with (
            patch.object(
                type(manager), "start", return_value={"id": "synthetic"}
            ) as start,
            patch.object(
                type(batches), "start", return_value={"id": "synthetic-batch"}
            ) as batch_start,
        ):
            self.assertEqual(
                client.post(
                    "/api/jobs",
                    json=self.credentials("Person@Example.com"),
                    headers=headers,
                ).status_code,
                202,
            )
            self.assert_limited(
                client.post(
                    "/api/batches",
                    json=self.batch("PERSON@example.com", "other@example.com"),
                    headers=headers,
                ),
                900,
            )
            self.assertEqual(
                client.post(
                    "/api/jobs",
                    json=self.credentials("other@example.com"),
                    headers=headers,
                ).status_code,
                202,
            )
            self.assertEqual(start.call_count, 2)
            batch_start.assert_not_called()

    def test_active_conflict_does_not_spend_account_budget(self):
        app, client, headers = self.app(account_attempts=Quota(1, 900))
        manager = workspace_for(app, client).jobs
        active = self.terminal_job(app, status="running")
        self.assertEqual(
            client.post(
                "/api/jobs", json=self.credentials(), headers=headers
            ).status_code,
            409,
        )
        manager.jobs.pop(active.id)
        with patch.object(type(manager), "start", return_value={"id": "synthetic"}):
            self.assertEqual(
                client.post(
                    "/api/jobs", json=self.credentials(), headers=headers
                ).status_code,
                202,
            )

    def test_failed_batch_start_refunds_the_whole_account_reservation(self):
        app, client, headers = self.app(account_attempts=Quota(1, 900))
        batches = workspace_for(app, client).batches
        payload = self.batch("person@example.com", "other@example.com")
        with patch.object(
            type(batches), "start", side_effect=OSError("synthetic failure")
        ):
            self.assertEqual(
                client.post("/api/batches", json=payload, headers=headers).status_code,
                500,
            )
        with patch.object(
            type(batches), "start", return_value={"id": "synthetic-batch"}
        ):
            self.assertEqual(
                client.post("/api/batches", json=payload, headers=headers).status_code,
                202,
            )

    def test_random_job_ids_do_not_bypass_the_shared_verification_budget(self):
        app, client, headers = self.app(verification=Quota(2, 60))
        for value in ("random-one", "random-two"):
            response = client.post(f"/api/jobs/{value}/code", json={"code": "123456", "challenge_id": value}, headers=headers)
            self.assertEqual(response.status_code, 404)
        self.assert_limited(client.post("/api/jobs/random-three/code", json={"code": "123456", "challenge_id": "new"}, headers=headers))
        keys = app.extensions["rt_abuse"].limiter.windows
        self.assertFalse(any(scope == "verify-job" for scope, _identity in keys))

    def test_existing_job_has_a_budget_independent_of_challenge_ids(self):
        app, client, headers = self.app(job_verification=Quota(1, 60))
        job = self.terminal_job(app, status="error")
        path = f"/api/jobs/{job.id}/code"
        self.assertEqual(client.post(path, json={"code": "123456", "challenge_id": "old"}, headers=headers).status_code, 409)
        self.assert_limited(client.post(path, json={"code": "654321", "challenge_id": "different"}, headers=headers))

    def test_every_result_endpoint_uses_the_same_export_budget(self):
        paths = [
            "/api/jobs/missing/result",
            "/api/batches/missing/results",
            "/api/jobs/missing/download",
            "/api/jobs/missing/rt",
            "/api/jobs/missing/cpa-download",
            "/api/jobs/missing/sub2api-download",
            "/api/batches/missing/download",
            "/api/batches/missing/rt-download",
            "/api/batches/missing/cpa-download",
            "/api/batches/missing/sub2api-download",
            "/api/batches/missing/conversion-source",
        ]
        for first_path in paths:
            with self.subTest(path=first_path):
                app, client, headers = self.app(exports=Quota(1, 60))
                response = client.get(first_path, headers=headers)
                self.assertEqual(response.status_code, 404)
                self.assert_limited(client.get("/api/jobs/another/rt", headers=headers))
                self.assertEqual(app.extensions["rt_abuse"]._exports, 0)

    def test_download_concurrency_releases_on_disconnect_exhaustion_and_head(self):
        app, client, headers = self.app(export_concurrency=1)
        job = self.terminal_job(app, size=100_000)
        path = f"/api/jobs/{job.id}/result"
        held = client.get(path, headers=headers, buffered=False)
        self.assertEqual(held.status_code, 200)
        self.assert_limited(client.get(path, headers=headers))
        held.close()
        full = client.get(path, headers=headers, buffered=False)
        self.assertEqual(full.status_code, 200)
        self.assertIn(b"synthetic-rt", full.get_data())
        self.assertEqual(app.extensions["rt_abuse"]._exports, 0)
        full.close()
        head = client.head(path, headers=headers)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(app.extensions["rt_abuse"]._exports, 0)
        head.close()

    def test_handoff_view_exception_and_stream_exception_release_concurrency(self):
        app, client, headers = self.app(export_concurrency=1)
        job = self.terminal_job(app)
        guard = app.extensions["rt_abuse"]
        path = f"/api/jobs/{job.id}/result"
        endpoint = app.url_map.bind("localhost").match(path)[0]
        original = app.view_functions[endpoint]

        def failure(**_kwargs):
            raise RuntimeError("synthetic view failure")

        app.view_functions[endpoint] = failure
        with self.assertRaises(RuntimeError):
            client.get(path, headers=headers)
        self.assertEqual(guard._exports, 0)

        def broken():
            yield b"first"
            raise RuntimeError("synthetic body failure")

        app.view_functions[endpoint] = lambda **kwargs: Response(
            broken(), direct_passthrough=True
        )
        response = client.get(path, headers=headers, buffered=False)
        with self.assertRaises(RuntimeError):
            response.get_data()
        response.close()
        self.assertEqual(guard._exports, 0)
        app.view_functions[endpoint] = original

    def test_small_write_endpoints_reject_oversized_bodies_before_processing(self):
        app, client, headers = self.app()
        for path, size in (("/api/jobs", 16 * 1024), ("/api/jobs/unknown/code", 4 * 1024),
                           ("/api/jobs/unknown/cancel", 1024), ("/api/batches/unknown/cancel", 1024),
                           ("/api/batches", 3 * 1024 * 1024)):
            with self.subTest(path=path):
                response = client.post(path, json={"padding": "x" * size}, headers=headers)
                self.assertEqual(response.status_code, 413)
                self.assertNotIn("xxx", response.get_data(as_text=True))

    def test_malformed_unicode_email_is_rejected_without_starting_a_job(self):
        app, client, headers = self.app()
        with patch.object(workspace_for(app, client).jobs, "start") as start:
            response = client.post(
                "/api/jobs",
                json=self.credentials("\ud800@example.com"),
                headers=headers,
            )
            self.assertEqual(response.status_code, 400)
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
