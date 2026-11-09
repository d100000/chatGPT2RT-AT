"""Email-challenge skipping contracts with synthetic credentials and gated workers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from rt_extractor.api import TokenBundle
from rt_extractor.web import create_app
from tests.test_abuse import policy
from tests.web_client import browser_headers, new_browser, workspace_for


SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "synthetic-skip-password-do-not-expose"


def bundle(email):
    return TokenBundle(
        email=email, access_token="synthetic-skip-at", refresh_token="synthetic-skip-rt",
        id_token="synthetic-skip-id", expires_in=3600,
        obtained_at="2026-11-09T00:00:00+00:00", expires_at="2026-11-09T01:00:00+00:00",
    )


async def wait_gate(gate):
    while not gate.is_set():
        await asyncio.sleep(.005)


class EmailSkipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name) / "must-not-be-created"
        self.calls = []
        self.gates = []

        async def successful(email, **_kwargs):
            return bundle(email)

        self.behavior = successful

        async def dispatch(email, **kwargs):
            self.calls.append(email)
            return await self.behavior(email, **kwargs)

        self.app = create_app(self.output, extractor=dispatch, abuse_policy=policy())
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.headers = browser_headers(self.app, self.client)

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        registry = self.app.extensions["rt_workspaces"]
        workspaces = list(registry.workspaces.values())
        for workspace in workspaces:
            for batch in list(workspace.batches.batches.values()):
                workspace.batches.cancel(batch.id)
            for job in list(workspace.jobs.jobs.values()):
                workspace.jobs.cancel(job.id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(not workspace.jobs.active_batch and all(
                job.status not in {"queued", "running", "awaiting_code"} and job.task is None
                for job in workspace.jobs.jobs.values()
            ) for workspace in workspaces):
                break
            time.sleep(.005)
        self.directory.cleanup()

    def gate(self):
        value = threading.Event()
        self.gates.append(value)
        return value

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers=self.headers)

    def start(self, emails, **settings):
        response = self.post("/api/batches", {
            "accounts_text": "\n".join(f"{email}----{PASSWORD}----{SECRET}" for email in emails),
            "concurrency": 1, **settings,
        })
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        return response.get_json()["id"]

    def wait(self, path, predicate):
        deadline = time.monotonic() + 5
        last = None
        while time.monotonic() < deadline:
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            last = response.get_json()
            if predicate(last):
                return last
            time.sleep(.005)
        self.fail(f"Synthetic job did not reach expected state: {last}")

    def wait_batch(self, batch_id, predicate):
        return self.wait(f"/api/batches/{batch_id}", predicate)

    def finished(self, batch_id):
        return self.wait_batch(batch_id, lambda state: state["status"] in {"completed", "cancelled"})

    def assert_skipped(self, job):
        self.assertEqual(job["status"], "skipped")
        self.assertIsNone(job["error"])
        self.assertIsNone(job["result"])
        text = json.dumps(job)
        for secret in (PASSWORD, SECRET, "synthetic-skip-at", "synthetic-skip-rt", "synthetic-skip-id"):
            self.assertNotIn(secret, text)

    def test_default_waits_and_manual_skip_advances_queue_without_result(self):
        received = []

        async def challenged(email, **kwargs):
            if email.startswith("email"):
                received.append(await kwargs["otp_fetcher"](email))
            return bundle(email)

        self.behavior = challenged
        batch_id = self.start(["email@example.com", "next@example.com"])
        waiting = self.wait_batch(batch_id, lambda state: state["counts"]["awaiting_code"] == 1)
        self.assertFalse(waiting["skip_email_verification"])
        self.assertEqual(waiting["counts"]["queued"], 1)
        job = waiting["active_jobs"][0]
        response = self.post(f"/api/jobs/{job['id']}/skip", {"challenge_id": job["challenge"]["id"]})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["challenge"])
        done = self.finished(batch_id)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["counts"]["skipped"], 1)
        self.assertEqual(done["counts"]["success"], 1)
        self.assertEqual(done["counts"]["error"], 0)
        self.assert_skipped(done["rows"][0])
        self.assertEqual(self.calls, ["email@example.com", "next@example.com"])
        self.assertEqual(received, [])
        records = self.client.get(f"/api/batches/{batch_id}/results", headers=self.headers).get_json()["records"]
        self.assertEqual([record["email"] for record in records], ["next@example.com"])
        workspace = workspace_for(self.app, self.client)
        self.assertIsNone(workspace.jobs.jobs[job["id"]].payload)
        self.assertEqual(workspace.batches.batches[batch_id].credential_records, [])
        self.assertFalse(self.output.exists())
        for path, payload in (
            (f"/api/jobs/{job['id']}/skip", {"challenge_id": job["challenge"]["id"]}),
            (f"/api/jobs/{job['id']}/code", {"challenge_id": job["challenge"]["id"], "code": "123456"}),
        ):
            self.assertEqual(self.post(path, payload).status_code, 409)

    def test_auto_skip_does_not_wait_and_preserves_successful_accounts(self):
        async def challenged(email, **kwargs):
            if email != "success@example.com":
                await kwargs["otp_fetcher"](email)
                self.fail("Skipped email verification must never supply a code")
            return bundle(email)

        self.behavior = challenged
        batch_id = self.start(["first@example.com", "success@example.com", "last@example.com"],
                              skip_email_verification=True)
        done = self.finished(batch_id)
        self.assertTrue(done["skip_email_verification"])
        self.assertEqual(done["counts"]["skipped"], 2)
        self.assertEqual(done["counts"]["success"], 1)
        self.assertEqual(done["counts"]["error"], 0)
        self.assertEqual(done["active_jobs"], [])
        self.assert_skipped(done["rows"][0])
        self.assert_skipped(done["rows"][2])
        self.assertEqual(self.calls, ["first@example.com", "success@example.com", "last@example.com"])

    def test_enable_skips_all_waiting_emails_and_disable_affects_future_challenge(self):
        third_release = self.gate()
        received = []

        async def challenged(email, **kwargs):
            if email == "third@example.com":
                await wait_gate(third_release)
            received.append((email, await kwargs["otp_fetcher"](email)))
            return bundle(email)

        self.behavior = challenged
        batch_id = self.start(["first@example.com", "second@example.com", "third@example.com"], concurrency=2)
        self.wait_batch(batch_id, lambda state: state["counts"]["awaiting_code"] == 2)
        response = self.post(f"/api/batches/{batch_id}/settings", {"skip_email_verification": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["skip_email_verification"])
        self.wait_batch(batch_id, lambda state: state["counts"]["skipped"] == 2)
        response = self.post(f"/api/batches/{batch_id}/settings", {"skip_email_verification": False})
        self.assertEqual(response.status_code, 200)
        third_release.set()
        waiting = self.wait_batch(batch_id, lambda state: state["counts"]["awaiting_code"] == 1)
        self.assertFalse(waiting["skip_email_verification"])
        job = waiting["active_jobs"][0]
        self.assertEqual(job["email"], "third@example.com")
        response = self.post(f"/api/jobs/{job['id']}/code", {"challenge_id": job["challenge"]["id"], "code": "123456"})
        self.assertEqual(response.status_code, 200)
        done = self.finished(batch_id)
        self.assertEqual(done["counts"]["skipped"], 2)
        self.assertEqual(done["counts"]["success"], 1)
        self.assertEqual(received, [("third@example.com", "123456")])
        self.assertNotIn("123456", json.dumps(done))
        self.assertEqual(self.post(f"/api/batches/{batch_id}/settings", {"skip_email_verification": True}).status_code, 409)

    def test_invalid_settings_are_rejected_before_accounts_start(self):
        for value in (None, "true", 0, 1, [], {}):
            with self.subTest(value=value):
                response = self.post("/api/batches", {
                    "accounts_text": f"invalid@example.com----{PASSWORD}----{SECRET}",
                    "skip_email_verification": value,
                })
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])

        async def challenged(email, **kwargs):
            await kwargs["otp_fetcher"](email)
            return bundle(email)

        self.behavior = challenged
        batch_id = self.start(["waiting@example.com"])
        waiting = self.wait_batch(batch_id, lambda state: state["counts"]["awaiting_code"] == 1)
        job = waiting["active_jobs"][0]
        for payload in ({}, [], {"skip_email_verification": "true"}, {"skip_email_verification": True, "extra": True}):
            self.assertEqual(self.post(f"/api/batches/{batch_id}/settings", payload).status_code, 400)
        for payload in ({}, [], {"challenge_id": 1}, {"challenge_id": "x" * 129},
                        {"challenge_id": job["challenge"]["id"], "extra": True}):
            self.assertEqual(self.post(f"/api/jobs/{job['id']}/skip", payload).status_code, 400)
        self.assertEqual(self.post(f"/api/jobs/{job['id']}/skip", {"challenge_id": "stale"}).status_code, 409)
        current = self.client.get(f"/api/batches/{batch_id}", headers=self.headers).get_json()
        self.assertFalse(current["skip_email_verification"])
        self.assertEqual(current["counts"]["awaiting_code"], 1)

    def test_skip_rejects_totp_and_allows_normal_verification(self):
        async def challenged(email, **kwargs):
            await kwargs["totp_fetcher"]()
            return bundle(email)

        self.behavior = challenged
        response = self.post("/api/jobs", {"email": "totp@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()["id"]
        job = self.wait(f"/api/jobs/{job_id}", lambda value: value["status"] == "awaiting_code")
        self.assertEqual(job["challenge"]["kind"], "totp")
        self.assertEqual(self.post(f"/api/jobs/{job_id}/skip", {"challenge_id": job["challenge"]["id"]}).status_code, 409)
        self.assertEqual(self.post(f"/api/jobs/{job_id}/code", {"challenge_id": job["challenge"]["id"], "code": "654321"}).status_code, 200)
        self.wait(f"/api/jobs/{job_id}", lambda value: value["status"] == "success")

    def test_new_routes_require_csrf_and_owning_browser(self):
        async def challenged(email, **kwargs):
            await kwargs["otp_fetcher"](email)
            return bundle(email)

        self.behavior = challenged
        batch_id = self.start(["private@example.com"])
        waiting = self.wait_batch(batch_id, lambda state: state["counts"]["awaiting_code"] == 1)
        job = waiting["active_jobs"][0]
        other, other_headers = new_browser(self.app)
        paths = (
            (f"/api/jobs/{job['id']}/skip", {"challenge_id": job["challenge"]["id"]}),
            (f"/api/batches/{batch_id}/settings", {"skip_email_verification": True}),
        )
        for path, payload in paths:
            self.assertEqual(self.client.post(path, json=payload).status_code, 403)
            response = other.post(path, json=payload, headers=other_headers)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(job["email"], response.get_data(as_text=True))
        self.assertEqual(self.post("/api/jobs/missing/skip", {"challenge_id": "missing"}).status_code, 404)
        self.assertEqual(self.post("/api/batches/missing/settings", {"skip_email_verification": True}).status_code, 404)

    def test_slot_remains_occupied_until_cleanup_and_swallowed_cancel_cannot_save(self):
        cleanup_entered, cleanup_release = self.gate(), self.gate()

        async def challenged(email, **kwargs):
            if email.startswith("first"):
                try:
                    await kwargs["otp_fetcher"](email)
                except asyncio.CancelledError:
                    cleanup_entered.set()
                    await wait_gate(cleanup_release)
                    return bundle(email)
            return bundle(email)

        self.behavior = challenged
        batch_id = self.start(["first@example.com", "next@example.com"])
        waiting = self.wait_batch(batch_id, lambda state: state["counts"]["awaiting_code"] == 1)
        job = waiting["active_jobs"][0]
        self.assertEqual(self.post(f"/api/jobs/{job['id']}/skip", {"challenge_id": job["challenge"]["id"]}).status_code, 200)
        self.assertTrue(cleanup_entered.wait(2))
        state = self.client.get(f"/api/batches/{batch_id}", headers=self.headers).get_json()
        self.assertEqual(state["counts"]["queued"], 1)
        self.assertEqual(state["counts"]["skipped"], 0)
        self.assertEqual(self.calls, ["first@example.com"])
        cleanup_release.set()
        done = self.finished(batch_id)
        self.assert_skipped(done["rows"][0])
        self.assertEqual(done["counts"]["success"], 1)

    def test_cleanup_exception_after_skip_still_finishes_as_skipped(self):
        async def challenged(email, **kwargs):
            try:
                await kwargs["otp_fetcher"](email)
            finally:
                raise RuntimeError(PASSWORD)

        self.behavior = challenged
        batch_id = self.start(["cleanup@example.com"], skip_email_verification=True)
        done = self.finished(batch_id)
        self.assert_skipped(done["rows"][0])
        self.assertEqual(done["counts"]["error"], 0)

    def test_batch_cancel_takes_priority_during_skip_cleanup(self):
        cleanup_entered, cleanup_release = self.gate(), self.gate()

        async def challenged(email, **kwargs):
            try:
                await kwargs["otp_fetcher"](email)
            except asyncio.CancelledError:
                cleanup_entered.set()
                await wait_gate(cleanup_release)
                return bundle(email)

        self.behavior = challenged
        batch_id = self.start(["first@example.com", "next@example.com"])
        waiting = self.wait_batch(batch_id, lambda state: state["counts"]["awaiting_code"] == 1)
        job = waiting["active_jobs"][0]
        self.assertEqual(self.post(f"/api/jobs/{job['id']}/skip", {"challenge_id": job["challenge"]["id"]}).status_code, 200)
        self.assertTrue(cleanup_entered.wait(2))
        self.assertEqual(self.post(f"/api/batches/{batch_id}/cancel", {}).status_code, 200)
        cleanup_release.set()
        done = self.finished(batch_id)
        self.assertEqual(done["status"], "cancelled")
        self.assertEqual(done["counts"]["cancelled"], 2)
        self.assertEqual(done["counts"]["skipped"], 0)
        self.assertEqual(done["counts"]["success"], 0)
        self.assertEqual(self.calls, ["first@example.com"])


if __name__ == "__main__":
    unittest.main()
