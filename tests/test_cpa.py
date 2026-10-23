"""CPA export and local converter integration, with synthetic credentials only."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from rt_extractor.api import TokenBundle
from rt_extractor.cpa import build_cpa_record
from rt_extractor.web import create_app
from tests.web_client import browser_headers, workspace_for, new_browser
from rt_extractor.web_jobs import Job


PASSWORD = "synthetic-cpa-password"
SECRET = "JBSWY3DPEHPK3PXP"


def bundle(email: str) -> TokenBundle:
    local = email.partition("@")[0]
    return TokenBundle(
        email=email, access_token=f"synthetic-at-{local}",
        refresh_token=f"synthetic-rt-{local}", id_token=f"synthetic-id-{local}",
        chatgpt_account_id=f"account-{local}", chatgpt_user_id=f"user-{local}",
        plan_type="plus", expires_in=3600,
        obtained_at="2026-10-22T01:00:00+00:00", expires_at="2026-10-22T02:00:00+00:00",
    )


def expected_record(email: str) -> dict:
    source = bundle(email)
    return {
        "type": "codex", "email": email,
        "access_token": source.access_token, "refresh_token": source.refresh_token,
        "id_token": source.id_token, "account_id": source.chatgpt_account_id,
        "plan_type": source.plan_type, "expired": source.expires_at,
        "last_refresh": source.obtained_at,
    }


class CPARecordTests(unittest.TestCase):
    def test_maps_actual_expiry_and_account_identity_without_exporting_other_secrets(self):
        record = bundle("first@example.com").to_dict()
        record.update(password=PASSWORD, totp_secret=SECRET, proxy="http://secret@localhost:7890", unrelated="private")
        before = dict(record)
        output = build_cpa_record(record)
        self.assertEqual(output, expected_record("first@example.com"))
        self.assertEqual(record, before, "An export must not mutate the saved token record")
        for secret in (PASSWORD, SECRET, "http://secret@localhost:7890", "private"):
            self.assertNotIn(secret, json.dumps(output))

    def test_missing_optional_metadata_is_omitted(self):
        record = {key: bundle("first@example.com").to_dict()[key]
                  for key in ("email", "access_token", "refresh_token", "id_token")}
        self.assertEqual(build_cpa_record(record), {"type": "codex", **record})

    def test_missing_or_invalid_required_values_are_rejected(self):
        for key in ("email", "access_token", "refresh_token", "id_token"):
            for value in (None, "", "   ", 42, []):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(ValueError):
                        build_cpa_record({**bundle("first@example.com").to_dict(), key: value})
        for record in (None, [], "synthetic-token"):
            with self.subTest(record=record), self.assertRaises(ValueError):
                build_cpa_record(record)


class CPAHandoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.batch_ids = []
        self.calls = []

        async def successful(email, **_kwargs):
            return bundle(email)

        self.extract = successful

        async def dispatch(email, **kwargs):
            self.calls.append(email)
            return await self.extract(email, **kwargs)

        self.app = create_app(self.output, extractor=dispatch)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.headers = browser_headers(self.app, self.client)

    def tearDown(self):
        for batch_id in self.batch_ids:
            response = self.client.get(f"/api/batches/{batch_id}", headers=self.headers)
            if response.status_code == 200 and response.get_json()["status"] not in {"completed", "cancelled"}:
                self.client.post(f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers)
                self.wait(f"/api/batches/{batch_id}", {"completed", "cancelled"})
        self.directory.cleanup()

    def wait(self, path, statuses):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(path, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            snapshot = response.get_json()
            if snapshot["status"] in statuses:
                return snapshot
            time.sleep(0.01)
        self.fail("Synthetic extraction did not reach the expected state")

    def start_batch(self, *emails):
        response = self.client.post("/api/batches", headers=self.headers, json={
            "accounts_text": "\n".join(f"{email}----{PASSWORD}----{SECRET}" for email in emails),
            "concurrency": 1,
        })
        self.assertEqual(response.status_code, 202)
        batch_id = response.get_json()["id"]
        self.batch_ids.append(batch_id)
        return batch_id

    def complete_batch(self, *emails):
        batch_id = self.start_batch(*emails)
        return self.wait(f"/api/batches/{batch_id}", {"completed"})

    def assert_no_secrets(self, value):
        text = json.dumps(value)
        for secret in (PASSWORD, SECRET, "synthetic-at-", "synthetic-rt-", "synthetic-id-"):
            self.assertNotIn(secret, text)

    def test_single_handoff_preserves_tokens_and_expiry_without_files(self):
        response = self.client.post(
            "/api/jobs",
            headers=self.headers,
            json={
                "email": "single@example.com",
                "password": PASSWORD,
                "totp_secret": SECRET,
            },
        )
        self.assertEqual(response.status_code, 202)
        job_id = response.get_json()["id"]
        job = self.wait(f"/api/jobs/{job_id}", {"success", "error"})
        self.assertEqual(job["status"], "success")
        self.assert_no_secrets(job)
        response = self.client.get(f"/api/jobs/{job_id}/result", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["records"][0]["cpa"],
            expected_record("single@example.com"),
        )
        self.assertTrue(response.cache_control.no_store)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_partial_batch_handoff_contains_only_successes_in_input_order(self):
        async def mixed(email, **_kwargs):
            if email.startswith("failed"):
                raise RuntimeError(f"private error {PASSWORD} {SECRET}")
            return bundle(email)

        self.extract = mixed
        batch = self.complete_batch(
            "first@example.com", "failed@example.com", "last@example.com"
        )
        response = self.client.get(
            f"/api/batches/{batch['id']}/results", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        records = response.get_json()["records"]
        self.assertEqual(
            [r["cpa"] for r in records],
            [expected_record("first@example.com"), expected_record("last@example.com")],
        )
        self.assertEqual([r["line"] for r in records], [1, 3])
        self.assert_no_secrets(batch)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cpa_downloads_require_csrf_same_origin_and_local_access(self):
        batch = self.complete_batch("guard@example.com")
        job_id = batch["rows"][0]["id"]
        options = [
            {}, {"headers": {"X-RT-CSRF": "wrong"}},
            {"headers": {**self.headers, "Origin": "https://untrusted.example"}},
            {"headers": self.headers, "environ_overrides": {"REMOTE_ADDR": "203.0.113.20"}},
        ]
        for path in (f"/api/jobs/{job_id}/cpa-download", f"/api/batches/{batch['id']}/cpa-download"):
            for kwargs in options:
                with self.subTest(path=path, options=kwargs):
                    response = self.client.get(path, **kwargs)
                    self.assertEqual(response.status_code, 403)
                    self.assert_no_secrets(response.get_json())

    def test_deprecated_exports_are_gone_for_owned_jobs_and_unknown_ids_stay_hidden(
        self,
    ):
        for path in (
            "/api/jobs/missing/cpa-download",
            "/api/batches/missing/cpa-download",
        ):
            self.assertEqual(
                self.client.get(path, headers=self.headers).status_code, 404
            )
        manager = workspace_for(self.app, self.client).jobs
        for status in ("queued", "running", "awaiting_code", "error", "cancelled"):
            job = Job(
                id=f"synthetic-{status}", email="pending@example.com", status=status
            )
            manager.jobs[job.id] = job
            response = self.client.get(
                f"/api/jobs/{job.id}/cpa-download", headers=self.headers
            )
            self.assertEqual(response.status_code, 410)
            self.assert_no_secrets(response.get_json())

    def test_completed_results_do_not_survive_server_restart(self):
        batch = self.complete_batch("first@example.com", "second@example.com")
        restarted = create_app(self.output, extractor=self.extract)
        client, headers = new_browser(restarted)
        self.assertIsNone(
            client.get("/api/batches/current", headers=headers).get_json()["batch"]
        )
        self.assertEqual(
            client.get(
                f"/api/batches/{batch['id']}/results", headers=headers
            ).status_code,
            404,
        )
        self.assertEqual(self.calls, ["first@example.com", "second@example.com"])

    def test_converter_page_and_local_modules_are_packaged_and_served(self):
        response = self.client.get("/converter")
        self.assertEqual(response.status_code, 200)
        self.assertIn("converter-ui.mjs", response.get_data(as_text=True))
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertTrue(response.cache_control.no_store)
        for asset in (
            "converter-ui.mjs",
            "converter.mjs",
            "archive.mjs",
            "paste-input.mjs",
        ):
            with self.subTest(asset=asset):
                response = self.client.get(f"/static/{asset}")
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    response.mimetype, {"text/javascript", "application/javascript"}
                )
                response.close()


if __name__ == "__main__":
    unittest.main()
