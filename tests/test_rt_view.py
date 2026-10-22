"""Explicit RT viewing contracts, with synthetic credentials and local fixtures."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from rt_extractor.api import TokenBundle
from rt_extractor.web import create_app
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
        self.headers = {"X-RT-CSRF": self.app.config["RT_CSRF_TOKEN"]}

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

    def test_success_returns_only_rt_and_public_status_stays_redacted(self):
        job = self.start_success()
        self.assert_no_secrets(job)
        response = self.client.get(f"/api/jobs/{job['id']}/rt", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.get_json(), {"refresh_token": RT})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("attachment", response.headers.get("Content-Disposition", ""))
        for path in ("/api/jobs/current", f"/api/jobs/{job['id']}"):
            self.assert_no_secrets(self.client.get(path, headers=self.headers).get_json())

    def test_view_requires_page_csrf_same_origin_and_local_client(self):
        job = self.start_success()
        path = f"/api/jobs/{job['id']}/rt"
        requests = [
            {}, {"headers": {"X-RT-CSRF": "wrong"}},
            {"headers": {**self.headers, "Origin": "https://untrusted.example"}},
            {"headers": self.headers, "environ_overrides": {"REMOTE_ADDR": "203.0.113.20"}},
        ]
        for options in requests:
            response = self.client.get(path, **options)
            self.assertEqual(response.status_code, 403)
            self.assert_no_secrets(response.get_json())

    def test_unknown_and_non_successful_jobs_cannot_reveal_rt(self):
        self.assertEqual(self.client.get("/api/jobs/missing/rt", headers=self.headers).status_code, 404)
        manager = self.app.extensions["rt_jobs"]
        for status in ("queued", "running", "awaiting_code", "error", "cancelled"):
            job = Job(id=f"synthetic-{status}", email=EMAIL, status=status)
            manager.jobs[job.id] = job
            with self.subTest(status=status):
                response = self.client.get(f"/api/jobs/{job.id}/rt", headers=self.headers)
                self.assertEqual(response.status_code, 409)
                self.assert_no_secrets(response.get_json())

    def test_missing_symlink_corrupt_and_unreadable_results_fail_safely(self):
        job = self.start_success()
        path = Path(job["result"]["path"])
        url = f"/api/jobs/{job['id']}/rt"
        path.unlink()
        self.assertEqual(self.client.get(url, headers=self.headers).status_code, 404)
        target = self.output / "synthetic-target.json"
        target.write_text(json.dumps({"refresh_token": RT}), encoding="utf-8")
        path.symlink_to(target)
        response = self.client.get(url, headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assert_no_secrets(response.get_json())
        path.unlink()
        for value in (None, 123, "", "   ", [RT]):
            path.write_text(json.dumps({"refresh_token": value}), encoding="utf-8")
            response = self.client.get(url, headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assert_no_secrets(response.get_json())
        for text in ('{"refresh_token": "' + RT, json.dumps([RT])):
            path.write_text(text, encoding="utf-8")
            response = self.client.get(url, headers=self.headers)
            self.assertEqual(response.status_code, 409)
            self.assert_no_secrets(response.get_json())
        path.write_text(json.dumps({"refresh_token": RT}), encoding="utf-8")
        with patch.object(Path, "read_text", side_effect=OSError(f"private read failure {RT}")):
            response = self.client.get(url, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assert_no_secrets(response.get_json())

    def test_completed_batch_rt_can_be_viewed_after_service_recovery(self):
        response = self.client.post(
            "/api/batches", json={"accounts_text": f"{EMAIL}----{PASSWORD}----{SECRET}"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)
        batch_id = response.get_json()["id"]
        done = self.wait(f"/api/batches/{batch_id}", {"completed"})
        self.assertEqual(done["counts"]["success"], 1)
        restored_app = create_app(self.output, extractor=successful)
        restored_client = restored_app.test_client()
        restored_headers = {"X-RT-CSRF": restored_app.config["RT_CSRF_TOKEN"]}
        state = restored_client.get("/api/batches/current", headers=restored_headers).get_json()["batch"]
        self.assertEqual(state["id"], batch_id)
        self.assert_no_secrets(state)
        job_id = state["rows"][0]["id"]
        response = restored_client.get(f"/api/jobs/{job_id}/rt", headers=restored_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"refresh_token": RT})


if __name__ == "__main__":
    unittest.main()
