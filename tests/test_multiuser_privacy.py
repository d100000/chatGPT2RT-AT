"""Multi-browser isolation and in-memory-only handoff, using synthetic accounts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rt_extractor.api import TokenBundle
from rt_extractor.browser_identity import DEVICE_COOKIE, SESSION_COOKIE
from rt_extractor.web import create_app
from tests.test_abuse import Clock, policy
from tests.web_client import browser_headers, new_browser


def bundle(email):
    return TokenBundle(
        email=email,
        access_token="synthetic-at:" + email,
        refresh_token="synthetic-rt:" + email,
        id_token="synthetic-id:" + email,
        expires_in=3600,
        obtained_at="2026-10-23T00:00:00+00:00",
        expires_at="2026-10-23T01:00:00+00:00",
    )


class MultiuserPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "must-not-be-created"
        self.clock = Clock()
        self.calls = []

        async def extract(email, **_kwargs):
            self.calls.append(email)
            return bundle(email)

        self.app = create_app(self.path, extractor=extract, abuse_policy=policy(), abuse_clock=self.clock)
        self.app.config["TESTING"] = True
        self.a, self.ah = new_browser(self.app)
        self.b, self.bh = new_browser(self.app)

    def tearDown(self):
        registry = self.app.extensions["rt_workspaces"]
        for workspace in list(registry.workspaces.values()):
            for job in list(workspace.jobs.jobs.values()):
                if job.status in {"queued", "running", "awaiting_code"}:
                    workspace.jobs.cancel(job.id)
        self.directory.cleanup()

    @staticmethod
    def input(email):
        return {
            "email": email,
            "password": "synthetic-private-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }

    def wait(self, client, headers, path, terminal):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            value = response.get_json()
            if value["status"] in terminal:
                return value
            time.sleep(0.01)
        self.fail("Synthetic task did not finish")

    def start_job(self, client=None, headers=None, email="alpha@example.com"):
        client, headers = client or self.a, headers or self.ah
        response = client.post("/api/jobs", headers=headers, json=self.input(email))
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job_id = response.get_json()["id"]
        return self.wait(client, headers, f"/api/jobs/{job_id}", {"success", "error"})

    def start_batch(self, client=None, headers=None, email="batch-alpha@example.com"):
        client, headers = client or self.a, headers or self.ah
        response = client.post(
            "/api/batches",
            headers=headers,
            json={"accounts_text": email + "----synthetic-private-password----JBSWY3DPEHPK3PXP"},
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        batch_id = response.get_json()["id"]
        return self.wait(client, headers, f"/api/batches/{batch_id}", {"completed", "cancelled"})

    def test_csrf_is_bound_to_the_signed_browser_session(self):
        self.assertNotEqual(self.ah, self.bh)
        self.assertNotEqual(self.a._rt_owner, self.b._rt_owner)
        for client, wrong in ((self.a, self.bh), (self.b, self.ah)):
            self.assertEqual(client.get("/api/jobs/current", headers=wrong).status_code, 403)
        self.assertEqual(self.a.get("/api/jobs/current", headers=self.ah).status_code, 200)

    def test_no_cookie_or_forged_cookie_never_gets_an_ip_shared_workspace(self):
        job = self.start_job()
        for mode in ("missing", "forged"):
            client = self.app.test_client()
            if mode == "forged":
                client.set_cookie(DEVICE_COOKIE, "forged-device")
                client.set_cookie(SESSION_COOKIE, "forged-session")
            for path in (
                "/api/jobs/current",
                f"/api/jobs/{job['id']}",
                f"/api/jobs/{job['id']}/result",
            ):
                response = client.get(path, headers=self.ah)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn("alpha@example.com", response.get_data(as_text=True))

    def test_two_browsers_on_one_ip_get_only_their_own_current_jobs(self):
        first = self.start_job()
        self.assertIsNone(self.b.get("/api/jobs/current", headers=self.bh).get_json()["job"])
        second = self.start_job(self.b, self.bh, "beta@example.com")
        for client, headers, expected, other in (
            (self.a, self.ah, first, second),
            (self.b, self.bh, second, first),
        ):
            current = client.get("/api/jobs/current", headers=headers).get_json()["job"]
            self.assertEqual(current["id"], expected["id"])
            self.assertNotIn(other["email"], json.dumps(current))

    def test_every_single_job_route_checks_owner_before_returning_data(self):
        job = self.start_job()
        paths = [
            ("GET", ""),
            ("GET", "/result"),
            ("GET", "/rt"),
            ("GET", "/download"),
            ("GET", "/cpa-download"),
            ("GET", "/sub2api-download"),
            ("POST", "/code"),
            ("POST", "/cancel"),
        ]
        for method, suffix in paths:
            with self.subTest(method=method, suffix=suffix):
                response = self.b.open(
                    f"/api/jobs/{job['id']}{suffix}",
                    method=method,
                    headers=self.bh,
                    json={"code": "123456", "challenge_id": "synthetic"} if suffix == "/code" else {},
                )
                self.assertEqual(response.status_code, 404, response.get_data(as_text=True))
                for secret in (job["email"], "synthetic-at", "synthetic-rt"):
                    self.assertNotIn(secret, response.get_data(as_text=True))

    def test_every_batch_route_checks_owner_and_foreign_ack_is_non_destructive(self):
        batch = self.start_batch()
        self.assertIsNone(self.b.get("/api/batches/current", headers=self.bh).get_json()["batch"])
        paths = [
            ("GET", ""),
            ("GET", "/results"),
            ("GET", "/download"),
            ("GET", "/rt-download"),
            ("GET", "/cpa-download"),
            ("GET", "/sub2api-download"),
            ("GET", "/conversion-source"),
            ("POST", "/cancel"),
        ]
        for method, suffix in paths:
            with self.subTest(method=method, suffix=suffix):
                response = self.b.open(
                    f"/api/batches/{batch['id']}{suffix}",
                    method=method,
                    headers=self.bh,
                    json={},
                )
                self.assertEqual(response.status_code, 404)
        self.b.post(
            f"/api/batches/{batch['id']}/ack",
            headers=self.bh,
            json={"ids": [batch["rows"][0]["id"]], "complete": True},
        )
        response = self.a.get(f"/api/batches/{batch['id']}/results", headers=self.ah)
        self.assertEqual(len(response.get_json()["records"]), 1)

    def test_handoff_is_retryable_until_ack_then_erases_server_tokens(self):
        job = self.start_job()
        endpoint = f"/api/jobs/{job['id']}/result"
        first = self.a.get(endpoint, headers=self.ah)
        again = self.a.get(endpoint, headers=self.ah)
        self.assertEqual(first.get_json()["records"], again.get_json()["records"])
        record = first.get_json()["records"][0]
        self.assertEqual(record["raw"], bundle(job["email"]).to_dict())
        self.assertEqual(record["cpa"]["refresh_token"], record["raw"]["refresh_token"])
        self.assertEqual(
            record["sub2api"]["accounts"][0]["credentials"]["access_token"],
            record["raw"]["access_token"],
        )
        response = self.a.post(
            f"/api/jobs/{job['id']}/ack",
            headers=self.ah,
            json={"ids": [job["id"]], "complete": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.a.get(endpoint, headers=self.ah).status_code, 404)
        self.assertIsNone(self.a.get("/api/jobs/current", headers=self.ah).get_json()["job"])
        registry = self.app.extensions["rt_workspaces"]
        self.assertNotIn("synthetic-rt", repr(registry.workspaces))

    def test_batch_ack_requires_all_records_before_clearing_completed_metadata(self):
        batch = self.start_batch()
        records = self.a.get(f"/api/batches/{batch['id']}/results", headers=self.ah).get_json()["records"]
        response = self.a.post(
            f"/api/batches/{batch['id']}/ack",
            headers=self.ah,
            json={"ids": [], "complete": True},
        )
        self.assertIn(response.status_code, (400, 409))
        self.assertEqual(
            len(self.a.get(f"/api/batches/{batch['id']}/results", headers=self.ah).get_json()["records"]),
            1,
        )
        response = self.a.post(
            f"/api/batches/{batch['id']}/ack",
            headers=self.ah,
            json={"ids": [records[0]["id"]], "complete": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.a.get("/api/batches/current", headers=self.ah).get_json()["batch"])
        # Retries after browser persistence do not fail or clear another batch.
        self.assertEqual(
            self.a.post(
                f"/api/batches/{batch['id']}/ack",
                headers=self.ah,
                json={"ids": [records[0]["id"]], "complete": True},
            ).status_code,
            200,
        )

    def test_ack_replay_and_clear_are_scoped_to_one_workspace(self):
        first = self.start_batch()
        second = self.start_batch(self.b, self.bh, "batch-beta@example.com")
        self.assertEqual(self.a.delete("/api/workspace", headers=self.ah).status_code, 200)
        self.assertIsNone(self.a.get("/api/batches/current", headers=self.ah).get_json()["batch"])
        self.assertEqual(
            self.b.get("/api/batches/current", headers=self.bh).get_json()["batch"]["id"],
            second["id"],
        )
        self.assertEqual(
            self.a.post(
                f"/api/batches/{first['id']}/ack",
                headers=self.ah,
                json={"ids": [], "complete": True},
            ).status_code,
            200,
        )
        self.assertEqual(
            len(self.b.get(f"/api/batches/{second['id']}/results", headers=self.bh).get_json()["records"]),
            1,
        )

    def test_web_never_creates_or_reads_the_old_output_directory(self):
        self.assertFalse(self.path.exists())
        with (
            patch(
                "rt_extractor.web_jobs.save_tokens",
                side_effect=AssertionError("web wrote Token file"),
            ),
            patch(
                "rt_extractor.web_jobs.save_export",
                side_effect=AssertionError("web wrote export"),
            ),
            patch(
                "rt_extractor.batch.save_export",
                side_effect=AssertionError("web wrote batch"),
            ),
        ):
            self.assertEqual(self.start_job()["status"], "success")
            self.assertEqual(self.start_batch(self.b, self.bh)["counts"]["success"], 1)
        self.assertFalse(self.path.exists())

    def test_process_restart_does_not_restore_saved_or_unacked_results(self):
        job = self.start_job()
        batch = self.start_batch(self.b, self.bh)
        self.path.mkdir()
        legacy_id, legacy_job_id = "d" * 32, "e" * 32
        legacy = self.path / f"batch-{legacy_id}"
        legacy.mkdir()
        # A valid old terminal manifest must be ignored, not merely rejected
        # because its shape is corrupt or its IDs are invalid.
        legacy_state = {
            "id": legacy_id,
            "status": "completed",
            "concurrency": 1,
            "rows": [
                {"id": legacy_job_id, "email": "someone-else@example.com", "line": 1, "status": "success", "result": {}}
            ],
        }
        (legacy / "batch-state.json").write_text(json.dumps(legacy_state))
        token_file = legacy / f"tokens-{legacy_job_id}.json"
        legacy_token = json.dumps(bundle("someone-else@example.com").to_dict())
        token_file.write_text(legacy_token)
        restarted = create_app(self.path, extractor=lambda **_kwargs: None, abuse_policy=policy())
        client, headers = new_browser(restarted)
        for endpoint in ("/api/jobs/current", "/api/batches/current"):
            payload = client.get(endpoint, headers=headers).get_json()
            self.assertTrue(all(value is None for value in payload.values()))
        for endpoint in (
            f"/api/jobs/{job['id']}/result",
            f"/api/batches/{batch['id']}/results",
            f"/api/batches/{legacy_id}/results",
            f"/api/jobs/{legacy_job_id}/result",
        ):
            self.assertEqual(client.get(endpoint, headers=headers).status_code, 404)
        self.assertEqual(token_file.read_text(), legacy_token)
        self.assertTrue(
            (legacy / "batch-state.json").exists(),
            "Existing private files must not be deleted silently",
        )

    def test_terminal_ttl_is_fixed_and_polling_does_not_keep_results_alive(self):
        job = self.start_job()
        self.clock.advance(599)
        self.assertEqual(
            self.a.get(f"/api/jobs/{job['id']}/result", headers=self.ah).status_code,
            200,
        )
        self.clock.advance(2)
        self.app.extensions["rt_workspaces"].cleanup()
        self.assertEqual(
            self.a.get(f"/api/jobs/{job['id']}/result", headers=self.ah).status_code,
            404,
        )

    def test_clearing_cookies_cannot_recover_the_previous_browser_results(self):
        job = self.start_job()
        self.a.delete_cookie(DEVICE_COOKIE)
        self.a.delete_cookie(SESSION_COOKIE)
        self.assertEqual(
            self.a.get(f"/api/jobs/{job['id']}/result", headers=self.ah).status_code,
            403,
        )
        headers = browser_headers(self.app, self.a, refresh=True)
        self.assertNotEqual(headers, self.ah)
        self.assertIsNone(self.a.get("/api/jobs/current", headers=headers).get_json()["job"])
        self.assertEqual(
            self.a.get(f"/api/jobs/{job['id']}/result", headers=headers).status_code,
            404,
        )

    def test_two_users_can_start_and_finish_without_a_global_current_conflict(self):
        started, release = threading.Event(), threading.Event()

        async def wait_extract(email, **_kwargs):
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.005)
            return bundle(email)

        self.app.extensions["rt_workspaces"].extractor = wait_extract
        try:
            one = self.a.post("/api/jobs", headers=self.ah, json=self.input("one@example.com"))
            self.assertEqual(one.status_code, 202)
            self.assertTrue(started.wait(1))
            two = self.b.post("/api/jobs", headers=self.bh, json=self.input("two@example.com"))
            self.assertEqual(two.status_code, 202)
        finally:
            release.set()
        for client, headers, response in (
            (self.a, self.ah, one),
            (self.b, self.bh, two),
        ):
            state = self.wait(
                client,
                headers,
                f"/api/jobs/{response.get_json()['id']}",
                {"success", "error"},
            )
            self.assertEqual(state["status"], "success")

    def test_app_and_web_requests_never_scan_or_create_a_token_directory(self):
        async def extract(email, **_kwargs):
            return bundle(email)

        with (
            patch.object(Path, "mkdir", side_effect=AssertionError("web created a directory")),
            patch.object(Path, "glob", side_effect=AssertionError("web scanned old tokens")),
            patch.object(Path, "rglob", side_effect=AssertionError("web scanned old tokens")),
            patch.object(Path, "read_text", side_effect=AssertionError("web read an old result")),
        ):
            app = create_app(self.path, extractor=extract, abuse_policy=policy())
            client, headers = new_browser(app)
            response = client.post("/api/jobs", json=self.input("zero-disk@example.com"), headers=headers)
            self.assertEqual(response.status_code, 202)
            job_id = response.get_json()["id"]
            done = self.wait(client, headers, f"/api/jobs/{job_id}", {"success", "error"})
            self.assertEqual(done["status"], "success")
            self.assertEqual(client.get(f"/api/jobs/{job_id}/result", headers=headers).status_code, 200)
            client.delete("/api/workspace", headers=headers)
        self.assertFalse(self.path.exists())

    def test_expired_signed_cookies_cannot_reuse_an_old_csrf_token(self):
        issued_at = 2_000_000_000
        with patch("itsdangerous.timed.time.time", return_value=issued_at):
            client, headers = new_browser(self.app)
        with patch("itsdangerous.timed.time.time", return_value=issued_at + 366 * 24 * 3600):
            response = client.get("/api/jobs/current", headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.headers.getlist("Set-Cookie"))

    def test_idle_active_workspace_is_cancelled_and_dropped_without_touching_other_users(self):
        entered = threading.Event()
        cancelled = threading.Event()

        async def waiting(email, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        registry = self.app.extensions["rt_workspaces"]
        registry.extractor = waiting
        response = self.a.post("/api/jobs", json=self.input("idle@example.com"), headers=self.ah)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(entered.wait(1))
        self.clock.advance(599)
        registry.ensure(self.b._rt_owner)
        self.clock.advance(2)
        registry.cleanup()
        self.assertTrue(cancelled.wait(2))
        self.assertNotIn(self.a._rt_owner, registry.workspaces)
        self.assertIn(self.b._rt_owner, registry.workspaces)
        self.assertIsNone(self.a.get("/api/jobs/current", headers=self.ah).get_json()["job"])

    def test_workspace_capacity_is_bounded_and_reading_does_not_allocate(self):
        registry = self.app.extensions["rt_workspaces"]
        registry.max_workspaces = 2
        for client, headers in ((self.a, self.ah), (self.b, self.bh)):
            self.assertIsNone(client.get("/api/jobs/current", headers=headers).get_json()["job"])
        self.assertEqual(len(registry.workspaces), 0)
        self.start_job()
        self.start_job(self.b, self.bh, "beta@example.com")
        third, headers = new_browser(self.app)
        response = third.post("/api/jobs", json=self.input("third@example.com"), headers=headers)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len(registry.workspaces), 2)
        self.assertNotIn(third._rt_owner, registry.workspaces)
        self.a.delete("/api/workspace", headers=self.ah)
        accepted = third.post("/api/jobs", json=self.input("third@example.com"), headers=headers)
        self.assertEqual(accepted.status_code, 202)
        done = self.wait(third, headers, f"/api/jobs/{accepted.get_json()['id']}", {"success", "error"})
        self.assertEqual(done["status"], "success")

    def test_record_and_workspace_memory_limits_do_not_publish_partial_tokens(self):
        registry = self.app.extensions["rt_workspaces"]
        workspace = registry.ensure(self.a._rt_owner)
        workspace.jobs.max_result_bytes = 10
        done = self.start_job()
        self.assertEqual(done["status"], "error")
        self.assertIsNone(done["result"])
        self.assertEqual(workspace.jobs.retained_bytes, 0)
        self.assertEqual(self.a.get(f"/api/jobs/{done['id']}/result", headers=self.ah).get_json()["records"], [])
        self.a.delete("/api/workspace", headers=self.ah)

        async def oversized(email, **_kwargs):
            value = bundle(email)
            value.access_token = "synthetic-large-" + "x" * (512 * 1024)
            return value

        registry.extractor = oversized
        response = self.a.post("/api/jobs", json=self.input("large@example.com"), headers=self.ah)
        self.assertEqual(response.status_code, 202)
        done = self.wait(self.a, self.ah, f"/api/jobs/{response.get_json()['id']}", {"success", "error"})
        self.assertEqual(done["status"], "error")
        self.assertIsNone(done["result"])
        self.assertNotIn("synthetic-large", json.dumps(done))
        self.assertFalse(self.path.exists())

    def test_running_batch_can_handoff_and_ack_a_finished_row_without_losing_later_rows(self):
        waiting = threading.Event()
        release = threading.Event()

        async def partial(email, **_kwargs):
            if email.startswith("second"):
                waiting.set()
                while not release.is_set():
                    await asyncio.sleep(0.005)
            return bundle(email)

        self.app.extensions["rt_workspaces"].extractor = partial
        response = self.a.post(
            "/api/batches",
            json={
                "concurrency": 1,
                "accounts_text": "\n".join(
                    f"{email}----synthetic-password----JBSWY3DPEHPK3PXP"
                    for email in ("first@example.com", "second@example.com")
                ),
            },
            headers=self.ah,
        )
        self.assertEqual(response.status_code, 202)
        batch_id = response.get_json()["id"]
        try:
            self.assertTrue(waiting.wait(1))
            first = self.a.get(f"/api/batches/{batch_id}/results", headers=self.ah).get_json()
            self.assertEqual([r["email"] for r in first["records"]], ["first@example.com"])
            first_id = first["records"][0]["id"]
            response = self.a.post(
                f"/api/batches/{batch_id}/ack", json={"ids": [first_id], "complete": False}, headers=self.ah
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.a.get(f"/api/batches/{batch_id}/results", headers=self.ah).get_json()["records"], [])
        finally:
            release.set()
        self.wait(self.a, self.ah, f"/api/batches/{batch_id}", {"completed"})
        remaining = self.a.get(f"/api/batches/{batch_id}/results", headers=self.ah).get_json()["records"]
        self.assertEqual([r["email"] for r in remaining], ["second@example.com"])
        self.assertEqual(
            self.a.post(
                f"/api/batches/{batch_id}/ack", json={"ids": [remaining[0]["id"]], "complete": True}, headers=self.ah
            ).status_code,
            200,
        )
        newer = self.start_batch(email="newer@example.com")
        self.assertEqual(
            self.a.post(
                f"/api/batches/{batch_id}/ack", json={"ids": [first_id], "complete": True}, headers=self.ah
            ).status_code,
            200,
        )
        self.assertEqual(self.a.get("/api/batches/current", headers=self.ah).get_json()["batch"]["id"], newer["id"])
        self.assertEqual(
            len(self.a.get(f"/api/batches/{newer['id']}/results", headers=self.ah).get_json()["records"]), 1
        )

    def test_new_session_on_the_same_device_has_a_separate_workspace(self):
        job = self.start_job()
        other = self.app.test_client()
        other.set_cookie(DEVICE_COOKIE, self.a.get_cookie(DEVICE_COOKIE).value)
        headers = browser_headers(self.app, other)
        self.assertNotEqual(headers, self.ah)
        self.assertNotEqual(other._rt_owner, self.a._rt_owner)
        self.assertEqual(other.get_cookie(DEVICE_COOKIE).value, self.a.get_cookie(DEVICE_COOKIE).value)
        self.assertIsNone(other.get("/api/jobs/current", headers=headers).get_json()["job"])
        self.assertEqual(other.get(f"/api/jobs/{job['id']}/result", headers=headers).status_code, 404)

    def test_ack_validation_is_atomic_when_ids_are_wrong_or_a_result_is_missing(self):
        response = self.a.post(
            "/api/batches",
            json={
                "accounts_text": "\n".join(
                    f"{email}----synthetic-password----JBSWY3DPEHPK3PXP"
                    for email in ("first@example.com", "second@example.com")
                )
            },
            headers=self.ah,
        )
        self.assertEqual(response.status_code, 202)
        batch_id = response.get_json()["id"]
        self.wait(self.a, self.ah, f"/api/batches/{batch_id}", {"completed"})
        records = self.a.get(f"/api/batches/{batch_id}/results", headers=self.ah).get_json()["records"]
        for payload, expected in (
            ({"ids": [records[0]["id"], "0" * 32], "complete": False}, 400),
            ({"ids": [records[0]["id"]], "complete": True}, 409),
            ({"ids": [records[0]["id"]], "complete": "true"}, 400),
        ):
            with self.subTest(payload=payload):
                response = self.a.post(f"/api/batches/{batch_id}/ack", json=payload, headers=self.ah)
                self.assertEqual(response.status_code, expected)
                self.assertEqual(
                    self.a.get(f"/api/batches/{batch_id}/results", headers=self.ah).get_json()["records"], records
                )

    def test_completed_row_expires_even_while_the_batch_keeps_working(self):
        entered, release = threading.Event(), threading.Event()

        async def partial(email, **_kwargs):
            if email.startswith("slow"):
                entered.set()
                while not release.is_set():
                    await asyncio.sleep(0.005)
            return bundle(email)

        registry = self.app.extensions["rt_workspaces"]
        registry.extractor = partial
        response = self.a.post(
            "/api/batches",
            json={
                "concurrency": 1,
                "accounts_text": "\n".join(
                    f"{email}----synthetic-password----JBSWY3DPEHPK3PXP"
                    for email in ("fast@example.com", "slow@example.com")
                ),
            },
            headers=self.ah,
        )
        self.assertEqual(response.status_code, 202)
        batch_id = response.get_json()["id"]
        try:
            self.assertTrue(entered.wait(1))
            self.clock.advance(599)
            before = self.a.get(f"/api/batches/{batch_id}/results", headers=self.ah).get_json()
            self.assertEqual([r["email"] for r in before["records"]], ["fast@example.com"])
            self.clock.advance(2)
            registry.cleanup()
            expired = self.a.get(f"/api/batches/{batch_id}/results", headers=self.ah).get_json()
            self.assertEqual(expired["records"], [])
            self.assertEqual(expired["batch"]["rows"][0]["result"]["delivery_status"], "expired")
        finally:
            release.set()
        self.wait(self.a, self.ah, f"/api/batches/{batch_id}", {"completed"})
        remaining = self.a.get(f"/api/batches/{batch_id}/results", headers=self.ah).get_json()["records"]
        self.assertEqual([r["email"] for r in remaining], ["slow@example.com"])

    def test_global_workers_are_bounded_across_independent_users(self):
        release = threading.Event()
        lock = threading.Lock()
        active, peak = 0, 0

        async def blocked(email, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                while not release.is_set():
                    await asyncio.sleep(0.005)
                return bundle(email)
            finally:
                with lock:
                    active -= 1

        registry = self.app.extensions["rt_workspaces"]
        registry.extractor = blocked
        third, headers = new_browser(self.app)
        browsers = [(self.a, self.ah), (self.b, self.bh), (third, headers)]
        batches = []
        try:
            for number, (client, headers) in enumerate(browsers):
                response = client.post(
                    "/api/batches",
                    json={
                        "concurrency": 3,
                        "accounts_text": "\n".join(
                            f"worker{number}-{row}@example.com----synthetic-password----JBSWY3DPEHPK3PXP"
                            for row in range(3)
                        ),
                    },
                    headers=headers,
                )
                self.assertEqual(response.status_code, 202)
                batches.append(response.get_json()["id"])
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with lock:
                    if active == registry.worker_limit:
                        break
                time.sleep(0.005)
            with lock:
                self.assertEqual(active, 6)
                self.assertEqual(peak, 6)
        finally:
            release.set()
        for (client, headers), batch_id in zip(browsers, batches):
            done = self.wait(client, headers, f"/api/batches/{batch_id}", {"completed"})
            self.assertEqual(done["counts"]["success"], 3)
        self.assertEqual(active, 0)
        self.assertLessEqual(peak, 6)

    def test_clear_waits_for_admission_and_cannot_leave_a_detached_task(self):
        from concurrent.futures import ThreadPoolExecutor

        registry = self.app.extensions["rt_workspaces"]
        allocated, release, delete_attempted = threading.Event(), threading.Event(), threading.Event()
        captured = []
        original_ensure = registry.ensure

        def hold_admission(owner):
            workspace = original_ensure(owner)
            captured.append(workspace)
            allocated.set()
            if not release.wait(3):
                raise AssertionError("Synthetic admission was not released")
            return workspace

        control = self.app.test_client()
        for name in (DEVICE_COOKIE, SESSION_COOKIE):
            control.set_cookie(name, self.a.get_cookie(name).value)

        def clear():
            delete_attempted.set()
            return control.delete("/api/workspace", headers=self.ah)

        with (
            patch.object(registry, "ensure", side_effect=hold_admission),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            started = executor.submit(self.a.post, "/api/jobs", json=self.input("racing@example.com"), headers=self.ah)
            try:
                self.assertTrue(allocated.wait(1))
                cleared = executor.submit(clear)
                self.assertTrue(delete_attempted.wait(1))
                # The request has entered while allocation is held. It must
                # not remove the workspace that is about to receive the job.
                time.sleep(0.05)
                clear_finished_before_admission = cleared.done()
            finally:
                release.set()
            start_response = started.result(timeout=3)
            clear_response = cleared.result(timeout=3)
        self.assertFalse(clear_finished_before_admission)
        self.assertEqual(start_response.status_code, 202)
        self.assertEqual(clear_response.status_code, 200)
        self.assertNotIn(self.a._rt_owner, registry.workspaces)
        self.assertEqual(captured[0].jobs.jobs, {})
        self.assertEqual(captured[0].jobs.retained_bytes, 0)
        self.assertIsNone(self.a.get("/api/jobs/current", headers=self.ah).get_json()["job"])

    def test_ack_empty_cleanup_cannot_delete_a_newly_admitted_job(self):
        from concurrent.futures import ThreadPoolExecutor

        old = self.start_job()
        registry = self.app.extensions["rt_workspaces"]
        ack_ready, resume_ack = threading.Event(), threading.Event()
        original_release = registry.release_empty

        def hold_cleanup(owner, **kwargs):
            ack_ready.set()
            if not resume_ack.wait(3):
                raise AssertionError("Synthetic ACK cleanup was not released")
            return original_release(owner, **kwargs)

        newer = self.app.test_client()
        for name in (DEVICE_COOKIE, SESSION_COOKIE):
            newer.set_cookie(name, self.a.get_cookie(name).value)
        with (
            patch.object(registry, "release_empty", side_effect=hold_cleanup),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            ack = executor.submit(
                self.a.post, f"/api/jobs/{old['id']}/ack", json={"ids": [old["id"]], "complete": True}, headers=self.ah
            )
            try:
                self.assertTrue(ack_ready.wait(1))
                response = newer.post("/api/jobs", json=self.input("new-after-ack@example.com"), headers=self.ah)
                self.assertEqual(response.status_code, 202)
                job_id = response.get_json()["id"]
                done = self.wait(newer, self.ah, f"/api/jobs/{job_id}", {"success", "error"})
                self.assertEqual(done["status"], "success")
            finally:
                resume_ack.set()
            self.assertEqual(ack.result(timeout=3).status_code, 200)
        current = newer.get("/api/jobs/current", headers=self.ah).get_json()["job"]
        self.assertEqual(current["id"], job_id)
        self.assertEqual(
            newer.get(f"/api/jobs/{job_id}/result", headers=self.ah).get_json()["records"][0]["email"],
            "new-after-ack@example.com",
        )

    def test_new_work_does_not_extend_old_job_or_batch_metadata_ttl(self):
        old_job = self.start_job(email="old-job@example.com")
        old_batch = self.start_batch(self.b, self.bh, "old-batch@example.com")
        old_row = old_batch["rows"][0]["id"]
        self.assertEqual(
            self.a.post(
                f"/api/jobs/{old_job['id']}/ack", json={"ids": [old_job["id"]], "complete": False}, headers=self.ah
            ).status_code,
            200,
        )
        self.assertEqual(
            self.b.post(
                f"/api/batches/{old_batch['id']}/ack", json={"ids": [old_row], "complete": False}, headers=self.bh
            ).status_code,
            200,
        )
        self.clock.advance(500)
        new_a = self.start_job(email="new-job-a@example.com")
        new_b = self.start_job(self.b, self.bh, "new-job-b@example.com")
        self.clock.advance(101)
        registry = self.app.extensions["rt_workspaces"]
        registry.cleanup()
        for client, headers, path in (
            (self.a, self.ah, f"/api/jobs/{old_job['id']}"),
            (self.b, self.bh, f"/api/jobs/{old_row}"),
            (self.b, self.bh, f"/api/batches/{old_batch['id']}"),
        ):
            self.assertEqual(client.get(path, headers=headers).status_code, 404)
        for client, headers, new in ((self.a, self.ah, new_a), (self.b, self.bh, new_b)):
            self.assertEqual(client.get("/api/jobs/current", headers=headers).get_json()["job"]["id"], new["id"])
            records = client.get(f"/api/jobs/{new['id']}/result", headers=headers).get_json()["records"]
            self.assertEqual([record["email"] for record in records], [new["email"]])
            workspace = registry.get(client._rt_owner)
            snapshot = json.dumps([job.snapshot() for job in workspace.jobs.jobs.values()])
            self.assertNotIn("old-job@example.com", snapshot)
            self.assertNotIn("old-batch@example.com", snapshot)
        self.assertIsNone(self.b.get("/api/batches/current", headers=self.bh).get_json()["batch"])


if __name__ == "__main__":
    unittest.main()
