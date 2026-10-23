"""Explicit RT viewing contracts, with synthetic credentials and local fixtures."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from rt_extractor.api import TokenBundle
from rt_extractor.web import create_app
from tests.web_client import browser_headers, workspace_for, new_browser
from rt_extractor.web_jobs import Job


EMAIL = "rt-view@example.com"
PASSWORD = "synthetic-rt-view-password"
SECRET = "JBSWY3DPEHPK3PXP"
RT = "synthetic-refresh-token-for-explicit-view"


async def successful(email, **_kwargs):
    return TokenBundle(
        email=email, access_token="synthetic-rt-view-access-token",
        refresh_token=RT, id_token="synthetic-rt-view-id-token",
        expires_in=3600, obtained_at="2026-10-22T01:00:00+00:00",
        expires_at="2026-10-22T02:00:00+00:00",
    )


class RTViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.app = create_app(self.output, extractor=successful)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.headers = browser_headers(self.app, self.client)

    def tearDown(self):
        self.directory.cleanup()

    def wait(self, path, statuses):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            state = response.get_json()
            if state["status"] in statuses:
                return state
            time.sleep(0.01)
        self.fail("Synthetic extraction did not finish")

    def start_success(self):
        response = self.client.post(
            "/api/jobs", json={"email": EMAIL, "password": PASSWORD, "totp_secret": SECRET},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()["id"]
        done = self.wait(f"/api/jobs/{job_id}", {"success", "error"})
        self.assertEqual(done["status"], "success")
        return done

    def assert_no_secrets(self, value):
        serialized = json.dumps(value)
        for secret in (PASSWORD, SECRET, RT, "synthetic-rt-view-access-token", "synthetic-rt-view-id-token"):
            self.assertNotIn(secret, serialized)

    def test_only_explicit_handoff_reveals_rt_while_public_status_stays_redacted(self):
        job = self.start_success()
        self.assert_no_secrets(job)
        response = self.client.get(
            f"/api/jobs/{job['id']}/result", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["records"][0]["raw"]["refresh_token"], RT)
        self.assertTrue(response.cache_control.no_store)
        self.assertEqual(list(self.output.iterdir()), [])
        for path in ("/api/jobs/current", f"/api/jobs/{job['id']}"):
            self.assert_no_secrets(
                self.client.get(path, headers=self.headers).get_json()
            )
        old = self.client.get(f"/api/jobs/{job['id']}/rt", headers=self.headers)
        self.assertEqual(old.status_code, 410)
        self.assert_no_secrets(old.get_json())

    def test_view_requires_page_csrf_same_origin_and_local_client(self):
        job = self.start_success()
        path = f"/api/jobs/{job['id']}/result"
        requests = [
            {},
            {"headers": {"X-RT-CSRF": "wrong"}},
            {"headers": {**self.headers, "Origin": "https://untrusted.example"}},
            {
                "headers": self.headers,
                "environ_overrides": {"REMOTE_ADDR": "203.0.113.20"},
            },
        ]
        for options in requests:
            response = self.client.get(path, **options)
            self.assertEqual(response.status_code, 403)
            self.assert_no_secrets(response.get_json())

    def test_unknown_and_unsuccessful_jobs_have_no_deliverable_rt(self):
        self.assertEqual(
            self.client.get(
                "/api/jobs/missing/result", headers=self.headers
            ).status_code,
            404,
        )
        manager = workspace_for(self.app, self.client).jobs
        for status in ("queued", "running", "awaiting_code", "error", "cancelled"):
            job = Job(id=f"synthetic-{status}", email=EMAIL, status=status)
            manager.jobs[job.id] = job
            response = self.client.get(
                f"/api/jobs/{job.id}/result", headers=self.headers
            )
            self.assertIn(response.status_code, (200, 409))
            self.assert_no_secrets(response.get_json())

    def test_restart_never_restores_previous_browsers_rt(self):
        job = self.start_success()
        restarted = create_app(self.output, extractor=successful)
        client, headers = new_browser(restarted)
        self.assertIsNone(
            client.get("/api/jobs/current", headers=headers).get_json()["job"]
        )
        self.assertEqual(
            client.get(f"/api/jobs/{job['id']}/result", headers=headers).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
