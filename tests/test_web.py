"""Local web UI tests: only injected extractors and synthetic credentials."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest

from rt_extractor.api import TokenBundle
from rt_extractor.abuse import AbusePolicy, Quota
from rt_extractor.web import create_app


EMAIL = "synthetic@example.com"
PASSWORD = "synthetic-password-do-not-expose"
SECRET = "JBSWY3DPEHPK3PXP"


def synthetic_bundle():
    return TokenBundle(
        email=EMAIL, access_token="synthetic-at-do-not-expose",
        refresh_token="synthetic-rt-do-not-expose", id_token="synthetic-id-do-not-expose",
        expires_in=3600, obtained_at="2026-10-22T01:00:00+00:00",
        expires_at="2026-10-22T02:00:00+00:00",
    )


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.directory.name)
        self.calls = []
        self.finished = threading.Event()

        async def default_extract(email, password, **kwargs):
            return synthetic_bundle()

        self.extract = default_extract

        async def dispatch(email, password, **kwargs):
            self.calls.append((email, password, kwargs))
            try:
                return await self.extract(email, password, **kwargs)
            finally:
                self.finished.set()

        # Input validation sends seven invalid requests in one window. Layered
        # rate-limit enforcement is covered separately in test_browser_limits.
        defaults = AbusePolicy()
        self.app = create_app(output_dir=self.output_dir, extractor=dispatch,
                              abuse_policy=replace(defaults, session=replace(
                                  defaults.session, submissions=Quota(10, 60))))
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.headers = {"X-RT-CSRF": self.app.config["RT_CSRF_TOKEN"]}

    def tearDown(self):
        current_response = self.client.get("/api/jobs/current", headers=self.headers)
        if current_response.status_code == 200:
            current = current_response.get_json().get("job")
            if current and current.get("status") not in {"success", "error", "cancelled"}:
                self.client.post(f"/api/jobs/{current['id']}/cancel", json={}, headers=self.headers)
                self.finished.wait(2)
        self.directory.cleanup()

    def job(self, response):
        payload = response.get_json()
        return payload.get("job", payload)

    def start(self, **changes):
        payload = {"email": EMAIL, "password": PASSWORD, "totp_secret": SECRET, **changes}
        response = self.client.post("/api/jobs", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return self.job(response)

    def wait_for(self, job_id, statuses):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/jobs/{job_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            job = self.job(response)
            if job["status"] in statuses:
                return job
            time.sleep(0.01)
        self.fail(f"Synthetic job did not reach {statuses}; last state was {job['status']}")

    def assert_public_safe(self, job):
        serialized = json.dumps(job)
        for value in (PASSWORD, SECRET, "synthetic-at-do-not-expose", "synthetic-rt-do-not-expose", "synthetic-id-do-not-expose"):
            self.assertNotIn(value, serialized)
        for key in ("password", "totp_secret", "access_token", "refresh_token", "id_token"):
            self.assertNotIn(key, job)

    def test_home_bootstrap_and_empty_current_job(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertRegex(html, r'<meta[^>]+name=["\']rt-csrf["\']')
        self.assertIn(self.app.config["RT_CSRF_TOKEN"], html)
        response = self.client.get("/api/jobs/current", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["job"])

    def test_api_requires_csrf_and_rejects_cross_origin_requests(self):
        no_token = self.client.post("/api/jobs", json={"email": EMAIL, "password": PASSWORD})
        self.assertEqual(no_token.status_code, 403)
        wrong_token = self.client.get("/api/jobs/current", headers={"X-RT-CSRF": "invalid"})
        self.assertEqual(wrong_token.status_code, 403)
        foreign = self.client.post(
            "/api/jobs", json={"email": EMAIL, "password": PASSWORD},
            headers={**self.headers, "Origin": "https://untrusted.example"},
        )
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_remote_host_and_client_cannot_access_local_ui(self):
        for host in ("untrusted.example", "127.0.0.1.untrusted.example"):
            response = self.client.get("/", headers={"Host": host})
            self.assertIn(response.status_code, (400, 403))
            self.assertNotIn(self.app.config["RT_CSRF_TOKEN"], response.get_data(as_text=True))
        response = self.client.get("/", environ_overrides={"REMOTE_ADDR": "203.0.113.20"})
        self.assertEqual(response.status_code, 403)

    def test_complete_result_download_and_private_file(self):
        job = self.start()
        done = self.wait_for(job["id"], {"success", "error"})
        self.assertEqual(done["status"], "success")
        self.assert_public_safe(done)
        current = self.client.get("/api/jobs/current", headers=self.headers).get_json()["job"]
        self.assertEqual(current["id"], job["id"])
        self.assert_public_safe(current)
        downloaded = self.client.get(f"/api/jobs/{job['id']}/download", headers=self.headers)
        self.assertEqual(downloaded.status_code, 200)
        self.assertIn("attachment", downloaded.headers.get("Content-Disposition", ""))
        self.assertEqual(json.loads(downloaded.get_data(as_text=True)), synthetic_bundle().to_dict())
        downloaded.close()
        files = list(self.output_dir.glob("tokens-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text()), synthetic_bundle().to_dict())
        if os.name == "posix":
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_invalid_inputs_never_start_extractor(self):
        cases = [
            {"email": "invalid"}, {"password": ""},
            {"totp_secret": "not-valid-***"}, {"timeout": 0},
            {"timeout": -1}, {"timeout": "not-a-number"},
            {"proxy": "ftp://127.0.0.1:21"},
        ]
        for changes in cases:
            response = self.client.post(
                "/api/jobs", json={"email": EMAIL, "password": PASSWORD, **changes},
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 400, str(changes))
            self.assertNotIn(PASSWORD, response.get_data(as_text=True))
        self.assertEqual(self.calls, [])

    def test_unexpected_error_is_redacted_and_no_result_is_saved(self):
        async def failing(_email, _password, **_kwargs):
            raise RuntimeError(f"secret leak {PASSWORD} {SECRET} synthetic-rt-do-not-expose")

        self.extract = failing
        job = self.start()
        done = self.wait_for(job["id"], {"error"})
        self.assert_public_safe(done)
        self.assertEqual(list(self.output_dir.glob("*.json")), [])
        unavailable = self.client.get(f"/api/jobs/{job['id']}/download", headers=self.headers)
        self.assertIn(unavailable.status_code, (404, 409))

    def test_totp_then_email_challenges_are_consumed_once(self):
        received = []

        async def challenged(email, _password, **kwargs):
            received.append(await kwargs["totp_fetcher"]())
            received.append(await kwargs["otp_fetcher"](email))
            return synthetic_bundle()

        self.extract = challenged
        job = self.start(totp_secret=None)
        waiting = self.wait_for(job["id"], {"awaiting_code"})
        first_challenge = waiting["challenge"]
        self.assertEqual(first_challenge["kind"], "totp")
        self.assert_public_safe(waiting)
        url = f"/api/jobs/{job['id']}/code"
        bad_code = self.client.post(url, json={"challenge_id": first_challenge["id"], "code": "12345"}, headers=self.headers)
        self.assertEqual(bad_code.status_code, 400)
        stale = self.client.post(url, json={"challenge_id": "stale-challenge", "code": "123456"}, headers=self.headers)
        self.assertEqual(stale.status_code, 409)
        accepted = self.client.post(url, json={"challenge_id": first_challenge["id"], "code": "012345"}, headers=self.headers)
        self.assertEqual(accepted.status_code, 200)
        replay = self.client.post(url, json={"challenge_id": first_challenge["id"], "code": "012345"}, headers=self.headers)
        self.assertEqual(replay.status_code, 409)
        second = self.wait_for(job["id"], {"awaiting_code"})
        self.assertEqual(second["challenge"]["kind"], "email")
        self.assertNotEqual(second["challenge"]["id"], first_challenge["id"])
        self.assertNotIn("012345", json.dumps(second))
        accepted = self.client.post(url, json={"challenge_id": second["challenge"]["id"], "code": "654321"}, headers=self.headers)
        self.assertEqual(accepted.status_code, 200)
        done = self.wait_for(job["id"], {"success", "error"})
        self.assertEqual(done["status"], "success")
        self.assertEqual(received, ["012345", "654321"])
        self.assertNotIn("654321", json.dumps(done))
        self.assert_public_safe(done)

    def test_one_active_job_and_cancellation_while_awaiting_code(self):
        async def challenged(_email, _password, **kwargs):
            await kwargs["totp_fetcher"]()
            return synthetic_bundle()

        self.extract = challenged
        job = self.start(totp_secret=None)
        waiting = self.wait_for(job["id"], {"awaiting_code"})
        concurrent = self.client.post(
            "/api/jobs", json={"email": "second@example.com", "password": "second-password"},
            headers=self.headers,
        )
        self.assertEqual(concurrent.status_code, 409)
        self.assertEqual(len(self.calls), 1)
        cancelled = self.client.post(f"/api/jobs/{job['id']}/cancel", json={}, headers=self.headers)
        self.assertEqual(cancelled.status_code, 200)
        done = self.wait_for(job["id"], {"cancelled"})
        self.assertIsNone(done["challenge"])
        self.assertTrue(self.finished.wait(1))
        self.assertEqual(list(self.output_dir.glob("*.json")), [])
        late = self.client.post(
            f"/api/jobs/{job['id']}/code",
            json={"challenge_id": waiting["challenge"]["id"], "code": "123456"}, headers=self.headers,
        )
        self.assertEqual(late.status_code, 409)
        self.assert_public_safe(done)

    def test_cancellation_prevents_saving_even_if_extractor_returns_after_cancel(self):
        entered = threading.Event()

        async def ignores_cancel(_email, _password, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return synthetic_bundle()

        self.extract = ignores_cancel
        job = self.start()
        self.assertTrue(entered.wait(1))
        self.client.post(f"/api/jobs/{job['id']}/cancel", json={}, headers=self.headers)
        self.wait_for(job["id"], {"cancelled"})
        self.assertEqual(list(self.output_dir.glob("*.json")), [])

    def test_non_ascii_csrf_is_rejected_cleanly(self):
        response = self.client.get("/api/jobs/current", headers={"X-RT-CSRF": "é"})
        self.assertEqual(response.status_code, 403)

    def test_non_ascii_challenge_id_is_rejected_cleanly(self):
        async def challenged(_email, _password, **kwargs):
            await kwargs["totp_fetcher"]()
            return synthetic_bundle()

        self.extract = challenged
        job = self.start(totp_secret=None)
        self.wait_for(job["id"], {"awaiting_code"})
        response = self.client.post(
            f"/api/jobs/{job['id']}/code", json={"challenge_id": "非ASCII", "code": "123456"},
            headers=self.headers,
        )
        self.assertIn(response.status_code, (400, 409))


if __name__ == "__main__":
    unittest.main()
