"""Batch HTTP contracts, using only synthetic credentials and injected extractors."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import unittest

from rt_extractor.api import TokenBundle
from rt_extractor.web import create_app
from tests.web_client import browser_headers


SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "synthetic-batch-password-do-not-expose"


def record(email: str, password: str = PASSWORD, secret: str = SECRET) -> str:
    return f"{email}----{password}----{secret}"


def bundle(email: str) -> TokenBundle:
    return TokenBundle(
        email=email, access_token=f"synthetic-at:{email}",
        refresh_token=f"synthetic-rt:{email}", id_token=f"synthetic-id:{email}",
        expires_in=3600, obtained_at="2026-10-22T01:00:00+00:00",
        expires_at="2026-10-22T02:00:00+00:00",
    )


class BatchWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.directory.name)
        self.calls = []
        self.batch_ids = []
        self.single_ids = []

        async def successful(email, _password, **_kwargs):
            return bundle(email)

        self.extract = successful

        async def dispatch(email, password, **kwargs):
            self.calls.append((email, password, kwargs))
            return await self.extract(email, password, **kwargs)

        self.app = create_app(output_dir=self.output_dir, extractor=dispatch)
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.headers = browser_headers(self.app, self.client)

    def tearDown(self):
        for batch_id in self.batch_ids:
            response = self.client.get(f"/api/batches/{batch_id}", headers=self.headers)
            if response.status_code == 200 and response.get_json()["status"] not in {"completed", "cancelled"}:
                self.client.post(f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers)
                self.wait_batch(batch_id, {"cancelled", "completed"})
        for job_id in self.single_ids:
            self.client.post(f"/api/jobs/{job_id}/cancel", json={}, headers=self.headers)
            self.wait_job(job_id, {"success", "error", "cancelled"})
        self.directory.cleanup()

    def start(self, lines: str, **kwargs):
        response = self.client.post(
            "/api/batches", json={"accounts_text": lines, "concurrency": 1, **kwargs}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        batch = response.get_json()
        self.batch_ids.append(batch["id"])
        return batch

    def wait_batch(self, batch_id, statuses):
        deadline = time.monotonic() + 5
        last = None
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/batches/{batch_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            last = response.get_json()
            if last["status"] in statuses:
                return last
            time.sleep(0.01)
        self.fail(f"Synthetic batch did not reach {statuses}; last status={last['status']}")

    def wait_job(self, job_id, statuses):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/jobs/{job_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            job = response.get_json()
            if job["status"] in statuses:
                return job
            time.sleep(0.01)
        self.fail("Synthetic single job did not finish")

    def assert_redacted(self, value):
        serialized = json.dumps(value, ensure_ascii=False)
        for secret in (PASSWORD, SECRET, "synthetic-at:", "synthetic-rt:", "synthetic-id:"):
            self.assertNotIn(secret, serialized)

    def handoff_tokens(self, batch_id):
        response = self.client.get(
            f"/api/batches/{batch_id}/results", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        return {
            f"{record['id']}.json": record["raw"]
            for record in response.get_json()["records"]
        }

    def test_empty_current_and_batch_endpoints_require_csrf(self):
        response = self.client.get("/api/batches/current", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["batch"])
        for path, method in (("/api/batches/current", "get"), ("/api/batches", "post")):
            response = getattr(self.client, method)(path)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_bom_blank_lines_normalization_and_password_spaces(self):
        text = "\ufeff\n" + record("  first@example.com  ", "  keep spaces  ", SECRET.lower()) + "\n\n" + record("second@example.com") + "\n"
        batch = self.start(text, proxy="http://127.0.0.1:7890", timeout=120)
        done = self.wait_batch(batch["id"], {"completed"})
        self.assertEqual([call[0] for call in self.calls], ["first@example.com", "second@example.com"])
        self.assertEqual(self.calls[0][1], "  keep spaces  ")
        self.assertEqual(self.calls[0][2]["totp_secret"], SECRET)
        self.assertEqual(self.calls[0][2]["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(self.calls[0][2]["timeout"], 120)
        self.assertEqual([row["line"] for row in done["rows"]], [2, 4])
        self.assertEqual(done["counts"]["success"], 2)
        self.assert_redacted(done)

    def test_validation_is_all_or_nothing_and_error_contains_only_line_and_reason(self):
        invalid_rows = [
            f"bad-address----{PASSWORD}----{SECRET}",
            f"bad@example.com--------{SECRET}",
            f"bad@example.com----{PASSWORD}----invalid-***",
            f"bad@example.com----{PASSWORD}",
            f"bad@example.com----{PASSWORD}----{SECRET}----extra",
            record("GOOD@example.com"),
        ]
        for invalid in invalid_rows:
            with self.subTest(invalid_kind=invalid.split("----")[0]):
                response = self.client.post(
                    "/api/batches", json={"accounts_text": record("good@example.com") + "\n" + invalid},
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 400)
                payload = response.get_json()
                self.assertTrue(payload.get("errors"))
                self.assertTrue(any(error["line"] == 2 for error in payload["errors"]))
                self.assert_redacted(payload)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.output_dir.rglob("*.json")), [])

    def test_invalid_container_and_explicit_1001_record_limit(self):
        payloads = [
            {"accounts_text": " \n\t"}, {"accounts_text": []},
            {"accounts_text": record("one@example.com"), "timeout": 0},
            {"accounts_text": record("one@example.com"), "proxy": "ftp://localhost"},
            {"accounts_text": "\n".join(record(f"person{i}@example.com") for i in range(1001))},
        ]
        for payload in payloads:
            response = self.client.post("/api/batches", json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assert_redacted(response.get_json())
        self.assertEqual(self.calls, [])

    def test_one_failure_does_not_stop_later_accounts_and_downloads_only_successes(
        self,
    ):
        async def mixed(email, _password, **_kwargs):
            if email.startswith("bad"):
                raise RuntimeError(f"response leaked {PASSWORD} {SECRET} synthetic-rt:bad@example.com")
            return bundle(email)

        self.extract = mixed
        emails = ["first@example.com", "bad@example.com", "last@example.com"]
        batch = self.start("\n".join(record(email) for email in emails))
        done = self.wait_batch(batch["id"], {"completed"})
        self.assertEqual([call[0] for call in self.calls], emails)
        self.assertEqual(done["counts"]["total"], 3)
        self.assertEqual(done["counts"]["success"], 2)
        self.assertEqual(done["counts"]["error"], 1)
        self.assertEqual(
            [row["status"] for row in done["rows"]], ["success", "error", "success"]
        )
        self.assert_redacted(done)
        documents = self.handoff_tokens(batch["id"])
        tokens = [
            document
            for document in documents.values()
            if isinstance(document, dict) and "refresh_token" in document
        ]
        self.assertEqual({token["email"] for token in tokens}, {emails[0], emails[2]})
        for token in tokens:
            self.assertEqual(token, bundle(token["email"]).to_dict())
        old = self.client.get(
            f"/api/batches/{batch['id']}/rt-download", headers=self.headers
        )
        self.assertEqual(old.status_code, 410)
        self.assert_redacted(old.get_json())

    def test_fifteen_rows_remain_isolated_in_memory_without_private_files(self):
        emails = [f"person{i}@example.com" for i in range(15)]
        batch = self.start("\n".join(record(email) for email in emails))
        done = self.wait_batch(batch["id"], {"completed"})
        self.assertEqual(done["counts"]["success"], len(emails))
        self.assertEqual(len(done["rows"]), len(emails))
        for row in done["rows"]:
            response = self.client.get(
                f"/api/jobs/{row['id']}/result", headers=self.headers
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.get_json()["records"][0]["raw"], bundle(row["email"]).to_dict()
            )
            self.assertNotIn("path", row["result"])
        self.assertEqual(list(self.output_dir.iterdir()), [])
        self.assert_redacted(done)
        self.assertEqual(len(self.handoff_tokens(batch["id"])), 15)
        denied = self.client.post(
            "/api/jobs",
            json={
                "email": "later-single@example.com",
                "password": PASSWORD,
                "totp_secret": SECRET,
            },
            headers=self.headers,
        )
        self.assertEqual(denied.status_code, 409)
        response = self.client.post(
            f"/api/batches/{batch['id']}/ack",
            json={"ids": [row["id"] for row in done["rows"]], "complete": True},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        single = self.client.post(
            "/api/jobs",
            json={
                "email": "later-single@example.com",
                "password": PASSWORD,
                "totp_secret": SECRET,
            },
            headers=self.headers,
        )
        self.assertEqual(single.status_code, 202)
        single_id = single.get_json()["id"]
        self.single_ids.append(single_id)
        self.wait_job(single_id, {"success"})
        self.assertEqual(
            self.client.get(
                f"/api/batches/{batch['id']}/results", headers=self.headers
            ).status_code,
            404,
        )

    def test_501_accounts_are_accepted_without_request_size_or_page_truncation(self):
        async def waiting(email, _password, **kwargs):
            await kwargs["otp_fetcher"](email)
            return bundle(email)

        self.extract = waiting
        batch = self.start("\n".join(record(f"person{i}@example.com") for i in range(501)))
        waiting_state = self.wait_batch(batch["id"], {"awaiting_code"})
        self.assertEqual(waiting_state["counts"]["total"], 501)
        self.assertEqual(len(waiting_state["rows"]), 501)
        self.assertEqual(waiting_state["rows"][-1]["email"], "person500@example.com")
        self.assertEqual(len(self.calls), 1)

    def test_extra_email_challenge_then_next_account(self):
        received = []

        async def challenged(email, _password, **kwargs):
            if email.startswith("first"):
                received.append(await kwargs["otp_fetcher"](email))
            return bundle(email)

        self.extract = challenged
        batch = self.start(record("first@example.com") + "\n" + record("second@example.com"))
        waiting = self.wait_batch(batch["id"], {"awaiting_code"})
        self.assertEqual(len(self.calls), 1)
        job = waiting["current"]
        self.assertEqual(job["challenge"]["kind"], "email")
        response = self.client.post(
            f"/api/jobs/{job['id']}/code", json={"challenge_id": job["challenge"]["id"], "code": "012345"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        done = self.wait_batch(batch["id"], {"completed"})
        self.assertEqual(received, ["012345"])
        self.assertEqual(done["counts"]["success"], 2)
        self.assertNotIn("012345", json.dumps(done))

    def test_cancel_keeps_success_downloads_and_never_starts_remaining_rows(self):
        async def challenged(email, _password, **kwargs):
            if email.startswith("second"):
                await kwargs["otp_fetcher"](email)
            return bundle(email)

        self.extract = challenged
        batch = self.start(
            "\n".join(
                record(email)
                for email in (
                    "first@example.com",
                    "second@example.com",
                    "third@example.com",
                )
            )
        )
        waiting = self.wait_batch(batch["id"], {"awaiting_code"})
        current = waiting["current"]
        pending_download = self.client.get(
            f"/api/batches/{batch['id']}/download", headers=self.headers
        )
        self.assertEqual(pending_download.status_code, 410)
        response = self.client.post(
            f"/api/batches/{batch['id']}/cancel", json={}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        done = self.wait_batch(batch["id"], {"cancelled"})
        self.assertEqual(done["counts"]["success"], 1)
        self.assertEqual(done["counts"]["cancelled"], 2)
        self.assertEqual(
            [call[0] for call in self.calls],
            ["first@example.com", "second@example.com"],
        )
        self.assertEqual(
            [row["status"] for row in done["rows"]],
            ["success", "cancelled", "cancelled"],
        )
        late = self.client.post(
            f"/api/jobs/{current['id']}/code",
            json={"challenge_id": current["challenge"]["id"], "code": "123456"},
            headers=self.headers,
        )
        self.assertEqual(late.status_code, 409)
        documents = self.handoff_tokens(batch["id"])
        tokens = [
            document
            for document in documents.values()
            if isinstance(document, dict) and "refresh_token" in document
        ]
        self.assertEqual(tokens, [bundle("first@example.com").to_dict()])
        self.assert_redacted(done)

    def test_single_job_and_batches_are_mutually_exclusive(self):
        async def challenged(email, _password, **kwargs):
            await kwargs["otp_fetcher"](email)
            return bundle(email)

        self.extract = challenged
        single = self.client.post(
            "/api/jobs", json={"email": "single@example.com", "password": PASSWORD, "totp_secret": SECRET},
            headers=self.headers,
        )
        self.assertEqual(single.status_code, 202)
        single_id = single.get_json()["id"]
        self.single_ids.append(single_id)
        self.wait_job(single_id, {"awaiting_code"})
        denied = self.client.post("/api/batches", json={"accounts_text": record("batch@example.com")}, headers=self.headers)
        self.assertEqual(denied.status_code, 409)
        self.client.post(f"/api/jobs/{single_id}/cancel", json={}, headers=self.headers)
        self.wait_job(single_id, {"cancelled"})
        batch = self.start(record("batch@example.com"))
        self.wait_batch(batch["id"], {"awaiting_code"})
        denied = self.client.post("/api/batches", json={"accounts_text": record("other@example.com")}, headers=self.headers)
        self.assertEqual(denied.status_code, 409)
        denied = self.client.post(
            "/api/jobs", json={"email": "other@example.com", "password": PASSWORD}, headers=self.headers,
        )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(len(self.calls), 2)

    def test_all_failed_batch_has_no_token_download(self):
        async def failing(_email, _password, **_kwargs):
            raise RuntimeError("synthetic failure")

        self.extract = failing
        batch = self.start(record("bad@example.com"))
        done = self.wait_batch(batch["id"], {"completed"})
        self.assertEqual(done["counts"]["error"], 1)
        self.assertEqual(done["counts"]["success"], 0)
        for suffix in ("download", "rt-download"):
            response = self.client.get(
                f"/api/batches/{batch['id']}/{suffix}", headers=self.headers
            )
            self.assertEqual(response.status_code, 410)


if __name__ == "__main__":
    unittest.main()
