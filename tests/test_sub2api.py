"""Sub2API schema and download contracts using synthetic account data only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from rt_extractor.api import TokenBundle
from rt_extractor.sub2api import build_sub2api_export
from rt_extractor.web import create_app


PASSWORD = "synthetic-sub2api-password"
SECRET = "JBSWY3DPEHPK3PXP"


def bundle(email: str) -> TokenBundle:
    local = email.partition("@")[0]
    return TokenBundle(
        email=email, access_token=f"synthetic-at-{local}",
        refresh_token=f"synthetic-rt-{local}", id_token=f"synthetic-id-{local}",
        chatgpt_account_id=f"account-{local}", chatgpt_user_id=f"user-{local}",
        expires_in=3600, obtained_at="2026-10-22T01:00:00+00:00",
        expires_at="2026-10-22T02:00:00+00:00",
    )


def expected_account(email: str) -> dict:
    source = bundle(email)
    return {
        "name": f"oauth---{email}", "platform": "openai", "type": "oauth",
        "credentials": {
            "access_token": source.access_token,
            "chatgpt_account_id": source.chatgpt_account_id,
            "email": email, "id_token": source.id_token,
            "refresh_token": source.refresh_token,
        },
        "extra": {
            "account_id": source.chatgpt_account_id,
            "chatgpt_account_id": source.chatgpt_account_id,
            "chatgpt_user_id": source.chatgpt_user_id,
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann", "email": email,
            "openai_long_context_billing_enabled": False,
            "openai_oauth_responses_websockets_v2_enabled": False,
            "openai_oauth_responses_websockets_v2_mode": "off",
            "privacy_mode": "training_set_failed",
        },
        "concurrency": 10, "priority": 1, "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


class Sub2APISchemaTests(unittest.TestCase):
    def test_exact_import_schema_with_distinct_accounts_and_no_unrelated_fields(self):
        emails = ["first@example.com", "second@example.com"]
        records = [bundle(email).to_dict() for email in emails]
        for record in records:
            record.update(password=PASSWORD, totp_secret=SECRET, proxy="http://secret@localhost:7890")
        document = build_sub2api_export(records)
        self.assertEqual(set(document), {"type", "version", "exported_at", "proxies", "accounts"})
        self.assertEqual(document["type"], "sub2api-data")
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["proxies"], [])
        self.assertRegex(document["exported_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual(document["accounts"], [expected_account(email) for email in emails])
        serialized = json.dumps(document)
        for value in (PASSWORD, SECRET, "http://secret@localhost:7890", "expires_at", "token_type"):
            self.assertNotIn(value, serialized)
        self.assertEqual(records[0]["password"], PASSWORD, "Formatting must not mutate source payloads")

    def test_missing_optional_identifiers_are_empty_without_borrowing_another_account(self):
        first = bundle("first@example.com").to_dict()
        second = bundle("second@example.com").to_dict()
        second.pop("chatgpt_account_id")
        second.pop("chatgpt_user_id")
        document = build_sub2api_export([first, second])
        account = document["accounts"][1]
        self.assertEqual(account["credentials"]["chatgpt_account_id"], "")
        self.assertEqual(account["extra"]["account_id"], "")
        self.assertEqual(account["extra"]["chatgpt_account_id"], "")
        self.assertEqual(account["extra"]["chatgpt_user_id"], "")
        self.assertEqual(account["credentials"]["refresh_token"], second["refresh_token"])

    def test_incomplete_records_reject_whole_export(self):
        good = bundle("first@example.com").to_dict()
        for key in ("email", "access_token", "refresh_token", "id_token"):
            for bad_value in (None, "", "   ", 42):
                with self.subTest(field=key, value_type=type(bad_value).__name__):
                    bad = {**bundle("second@example.com").to_dict(), key: bad_value}
                    with self.assertRaises(ValueError):
                        build_sub2api_export([good, bad])
        with self.assertRaises(ValueError):
            build_sub2api_export([])


class Sub2APIDownloadTests(unittest.TestCase):
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
        self.headers = {"X-RT-CSRF": self.app.config["RT_CSRF_TOKEN"]}

    def tearDown(self):
        for batch_id in self.batch_ids:
            response = self.client.get(f"/api/batches/{batch_id}", headers=self.headers)
            if response.status_code == 200 and response.get_json()["status"] not in {"completed", "cancelled"}:
                self.client.post(f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers)
                self.wait(batch_id, {"completed", "cancelled"})
        self.directory.cleanup()

    def start(self, *emails):
        lines = "\n".join(f"{email}----{PASSWORD}----{SECRET}" for email in emails)
        response = self.client.post("/api/batches", json={"accounts_text": lines, "concurrency": 1}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        batch_id = response.get_json()["id"]
        self.batch_ids.append(batch_id)
        return batch_id

    def wait(self, batch_id, statuses):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/batches/{batch_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            snapshot = response.get_json()
            if snapshot["status"] in statuses:
                return snapshot
            time.sleep(0.01)
        self.fail("Synthetic batch failed to reach the expected state")

    def download(self, batch_id, *, client=None, headers=None):
        response = (client or self.client).get(
            f"/api/batches/{batch_id}/sub2api-download", headers=headers or self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        self.assertIn(f"sub2api-{batch_id}.json", response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        document = json.loads(response.get_data())
        response.close()
        return document

    def test_partial_success_export_preserves_input_order_and_exact_account_mapping(self):
        async def mixed(email, **_kwargs):
            if email.startswith("failed"):
                raise RuntimeError(f"should be hidden: {PASSWORD} {SECRET}")
            return bundle(email)

        self.extract = mixed
        batch_id = self.start("first@example.com", "failed@example.com", "last@example.com")
        snapshot = self.wait(batch_id, {"completed"})
        self.assertEqual(snapshot["result"]["sub2api_url"], f"/api/batches/{batch_id}/sub2api-download")
        self.assertEqual(snapshot["counts"]["success"], 2)
        document = self.download(batch_id)
        self.assertEqual(document["accounts"], [expected_account("first@example.com"), expected_account("last@example.com")])
        for secret in (PASSWORD, SECRET, "failed@example.com"):
            self.assertNotIn(secret, json.dumps(document))
        self.assertNotIn("synthetic-at-", json.dumps(snapshot))

    def test_download_requires_csrf_and_a_finished_batch_with_successes(self):
        async def awaiting(email, **kwargs):
            await kwargs["otp_fetcher"](email)
            return bundle(email)

        self.extract = awaiting
        batch_id = self.start("waiting@example.com")
        self.wait(batch_id, {"awaiting_code"})
        endpoint = f"/api/batches/{batch_id}/sub2api-download"
        self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.client.get(endpoint, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.get("/api/batches/missing/sub2api-download", headers=self.headers).status_code, 404)
        self.client.post(f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers)
        self.wait(batch_id, {"cancelled"})
        self.assertEqual(self.client.get(endpoint, headers=self.headers).status_code, 409)

    def test_completed_batch_and_download_survive_restart_without_logging_in_again(self):
        batch_id = self.start("first@example.com", "second@example.com")
        finished = self.wait(batch_id, {"completed"})
        expected = self.download(batch_id)
        state_file = self.output / f"batch-{batch_id}" / "batch-state.json"
        self.assertTrue(state_file.is_file())
        state_text = state_file.read_text(encoding="utf-8")
        for secret in (PASSWORD, SECRET, "synthetic-at-", "synthetic-rt-", "synthetic-id-"):
            self.assertNotIn(secret, state_text)
        if os.name == "posix":
            self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)
        # A stored public snapshot must never control the file or URL served.
        saved = json.loads(state_text)
        for row in saved["rows"]:
            row["result"]["path"] = "/tmp/not-this-batch.json"
            row["result"]["filename"] = "../../not-this-batch.json"
            row["result"]["download_url"] = "https://invalid.example/token"
        state_file.write_text(json.dumps(saved), encoding="utf-8")

        async def forbidden(**_kwargs):
            self.fail("Restoring completed results must not authenticate accounts again")

        restarted = create_app(self.output, extractor=forbidden)
        client = restarted.test_client()
        headers = {"X-RT-CSRF": restarted.config["RT_CSRF_TOKEN"]}
        current = client.get("/api/batches/current", headers=headers).get_json()["batch"]
        self.assertEqual(current["id"], batch_id)
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["counts"], finished["counts"])
        self.assertEqual(self.download(batch_id, client=client, headers=headers)["accounts"], expected["accounts"])
        for row in current["rows"]:
            self.assertEqual(Path(row["result"]["path"]).resolve(), (state_file.parent / f"tokens-{row['id']}.json").resolve())
            self.assertEqual(row["result"]["download_url"], f"/api/jobs/{row['id']}/download")
            response = client.get(row["result"]["download_url"], headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(response.get_data()), bundle(row["email"]).to_dict())
            response.close()

    def test_cancelled_batch_can_export_previously_completed_accounts_after_restart(self):
        async def partial(email, **kwargs):
            if email.startswith("waiting"):
                await kwargs["otp_fetcher"](email)
            return bundle(email)

        self.extract = partial
        batch_id = self.start("first@example.com", "waiting@example.com", "last@example.com")
        self.wait(batch_id, {"awaiting_code"})
        self.client.post(f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers)
        finished = self.wait(batch_id, {"cancelled"})
        self.assertEqual(finished["counts"]["success"], 1)
        restarted = create_app(self.output, extractor=self.extract)
        client = restarted.test_client()
        headers = {"X-RT-CSRF": restarted.config["RT_CSRF_TOKEN"]}
        self.assertEqual(self.download(batch_id, client=client, headers=headers)["accounts"], [expected_account("first@example.com")])

    def test_corrupt_or_nonterminal_saved_batches_are_ignored_without_resuming(self):
        batch_id = self.start("first@example.com")
        finished = self.wait(batch_id, {"completed"})
        state_file = self.output / f"batch-{batch_id}" / "batch-state.json"
        active_row = json.loads(json.dumps(finished))
        active_row["rows"][0]["status"] = "running"
        malformed_states = [
            "{not-json", json.dumps([]),
            json.dumps({**finished, "status": "running"}), json.dumps(active_row),
        ]
        for malformed in malformed_states:
            with self.subTest(malformed=malformed[:30]):
                state_file.write_text(malformed, encoding="utf-8")
                restarted = create_app(self.output, extractor=self.extract)
                client = restarted.test_client()
                headers = {"X-RT-CSRF": restarted.config["RT_CSRF_TOKEN"]}
                response = client.get("/api/batches/current", headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.get_json()["batch"])
                self.assertFalse(restarted.extensions["rt_jobs"].active_batch)
        self.assertEqual(self.calls, ["first@example.com"])


if __name__ == "__main__":
    unittest.main()
