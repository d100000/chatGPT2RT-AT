"""Automatic CPA/Sub2API output lifecycle using synthetic extraction only."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from rt_extractor.api import TokenBundle
from rt_extractor.cpa import build_cpa_record, save_export
from rt_extractor.sub2api import build_sub2api_export
from rt_extractor.web import create_app


EMAIL = "synthetic-auto@example.com"
PASSWORD = "synthetic-password-not-in-artifacts"
SECRET = "JBSWY3DPEHPK3PXP"


def bundle(email=EMAIL):
    return TokenBundle(
        email=email, access_token=f"synthetic-at-{email}",
        refresh_token=f"synthetic-rt-{email}", id_token=f"synthetic-id-{email}",
        expires_in=3600, obtained_at="2026-10-22T01:00:00+00:00",
        expires_at="2026-10-22T02:00:00+00:00", chatgpt_account_id="synthetic-account",
        chatgpt_user_id="synthetic-user", plan_type="plus",
    )


class AutomaticExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name).resolve()

        async def extract(email, **_kwargs):
            return bundle(email)

        self.app = create_app(self.output, extractor=extract)
        self.client = self.app.test_client()
        self.headers = {"X-RT-CSRF": self.app.config["RT_CSRF_TOKEN"]}

    def tearDown(self):
        self.directory.cleanup()

    def wait(self, endpoint, terminal):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(endpoint, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            if data["status"] in terminal:
                return data
            time.sleep(0.01)
        self.fail("Synthetic job did not finish")

    def start_single(self):
        response = self.client.post("/api/jobs", headers=self.headers, json={
            "email": EMAIL, "password": PASSWORD, "totp_secret": SECRET,
        })
        self.assertEqual(response.status_code, 202)
        return self.wait(f"/api/jobs/{response.get_json()['id']}", {"success", "error"})

    def start_batch(self, *emails):
        response = self.client.post("/api/batches", headers=self.headers, json={
            "accounts_text": "\n".join(f"{email}----{PASSWORD}----{SECRET}" for email in emails),
            "concurrency": 1,
        })
        self.assertEqual(response.status_code, 202)
        return self.wait(f"/api/batches/{response.get_json()['id']}", {"completed", "cancelled"})

    def assert_private(self, path):
        self.assertFalse(path.is_symlink())
        self.assertTrue(path.is_file())
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def assert_public(self, snapshot):
        text = json.dumps(snapshot)
        for secret in (PASSWORD, SECRET, "synthetic-at-", "synthetic-rt-", "synthetic-id-"):
            self.assertNotIn(secret, text)

    def test_generated_file_publication_refuses_overwrite_and_symlinks(self):
        target = self.output / "synthetic-export.json"
        save_export(b'{"safe":"original"}', target)
        self.assert_private(target)
        with self.assertRaises(FileExistsError):
            save_export(b'{"safe":"replacement"}', target)
        link = self.output / "synthetic-link.json"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            save_export(b'{"safe":"replacement"}', link)
        self.assertEqual(target.read_bytes(), b'{"safe":"original"}')

    def test_failed_atomic_publication_cleans_temporary_file(self):
        target = self.output / "synthetic-export.json"
        with patch("rt_extractor.cpa.os.link", side_effect=OSError("synthetic publication failure")):
            with self.assertRaises(OSError):
                save_export(b'{"safe":"synthetic"}', target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_success_automatically_publishes_private_cpa_and_sub2api_without_download(self):
        job = self.start_single()
        self.assertEqual(job["status"], "success")
        result = job["result"]
        self.assertEqual(result["conversion_status"], "ready")
        raw_path = self.output / f"tokens-{job['id']}.json"
        cpa_path = self.output / f"codex-{job['id']}.json"
        sub_path = self.output / f"sub2api-{job['id']}.json"
        for path in (raw_path, cpa_path, sub_path):
            self.assert_private(path)
            for secret in (PASSWORD, SECRET):
                self.assertNotIn(secret, path.read_text())
        self.assertEqual(json.loads(cpa_path.read_text()), build_cpa_record(bundle().to_dict()))
        sub = json.loads(sub_path.read_text())
        self.assertEqual(sub["accounts"], build_sub2api_export([bundle().to_dict()])["accounts"])
        self.assertEqual(result["cpa_path"], str(cpa_path))
        self.assertEqual(result["sub2api_path"], str(sub_path))
        self.assert_public(job)
        self.assertEqual(list(self.output.glob("*.tmp")), [])

    def test_generated_single_sub2api_download_matches_saved_credentials_and_guards(self):
        job = self.start_single()
        endpoint = job["result"]["sub2api_url"]
        self.assertEqual(endpoint, f"/api/jobs/{job['id']}/sub2api-download")
        response = self.client.get(endpoint, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(f"sub2api-{job['id']}.json", response.headers["Content-Disposition"])
        self.assertEqual(response.get_json()["accounts"], build_sub2api_export([bundle().to_dict()])["accounts"])
        response.close()
        self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.client.get("/api/jobs/missing/sub2api-download", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.get(endpoint, headers={**self.headers, "Origin": "https://untrusted.example"}).status_code, 403)
        Path(job["result"]["path"]).write_text('{"refresh_token":"synthetic-private"}', encoding="utf-8")
        self.assertEqual(self.client.get(endpoint, headers=self.headers).status_code, 409)

    def test_conversion_save_failure_does_not_publish_success_or_partial_formats(self):
        from rt_extractor.cpa import save_export
        calls = []

        def fail_second(data, output):
            calls.append(Path(output))
            if len(calls) == 2:
                raise OSError(f"private failure {PASSWORD} {SECRET}")
            return save_export(data, output)

        with patch("rt_extractor.web_jobs.save_export", side_effect=fail_second):
            job = self.start_single()
        self.assertEqual(job["status"], "error")
        self.assertIn("自动生成失败", job["error"])
        self.assertIsNone(job["result"])
        self.assert_private(self.output / f"tokens-{job['id']}.json")
        self.assertFalse((self.output / f"codex-{job['id']}.json").exists())
        self.assertFalse((self.output / f"sub2api-{job['id']}.json").exists())
        self.assert_public(job)
        self.assertEqual(self.client.get(f"/api/jobs/{job['id']}/sub2api-download", headers=self.headers).status_code, 409)

    def test_completed_batch_automatically_saves_both_formats_and_restores_safe_paths(self):
        emails = ("first@example.com", "last@example.com")
        batch = self.start_batch(*emails)
        result = batch["result"]
        self.assertEqual(result["conversion_status"], "ready")
        directory = self.output / f"batch-{batch['id']}"
        cpa = directory / f"cpa-{batch['id']}.zip"
        sub = directory / f"sub2api-{batch['id']}.json"
        for path in (cpa, sub):
            self.assert_private(path)
        with zipfile.ZipFile(cpa) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual([json.loads(z.read(name))["email"] for name in z.namelist()], list(emails))
        self.assertEqual([a["credentials"]["email"] for a in json.loads(sub.read_text())["accounts"]], list(emails))
        state_file = directory / "batch-state.json"
        state = json.loads(state_file.read_text())
        for row in state["rows"]:
            for key in ("cpa_path", "sub2api_path", "cpa_url", "sub2api_url"):
                row["result"][key] = "https://untrusted.example/private"
        state_file.write_text(json.dumps(state))
        restarted = create_app(self.output)
        client = restarted.test_client()
        headers = {"X-RT-CSRF": restarted.config["RT_CSRF_TOKEN"]}
        restored = client.get("/api/batches/current", headers=headers).get_json()["batch"]
        self.assertEqual(restored["result"]["conversion_status"], "ready")
        for row in restored["rows"]:
            self.assertEqual(row["result"]["conversion_status"], "ready")
            self.assertEqual(row["result"]["cpa_path"], str(directory / f"codex-{row['id']}.json"))
            self.assertEqual(row["result"]["sub2api_url"], f"/api/jobs/{row['id']}/sub2api-download")
            response = client.get(row["result"]["sub2api_url"], headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["accounts"][0]["credentials"]["email"], row["email"])
            response.close()
        self.assert_public(restored)

    def test_batch_aggregate_failure_is_explicit_and_keeps_individual_formats(self):
        with patch("rt_extractor.batch.save_export", side_effect=OSError("private aggregate write failure")):
            batch = self.start_batch(EMAIL)
        self.assertEqual(batch["counts"]["success"], 1)
        self.assertEqual(batch["result"]["conversion_status"], "error")
        self.assertIn("自动生成失败", batch["result"]["conversion_error"])
        self.assertEqual(batch["error"], batch["result"]["conversion_error"])
        self.assert_public(batch)
        for row in batch["rows"]:
            self.assert_private(Path(row["result"]["cpa_path"]))
            self.assert_private(Path(row["result"]["sub2api_path"]))
        for endpoint in (batch["result"]["cpa_url"], batch["result"]["sub2api_url"]):
            response = self.client.get(endpoint, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            response.close()
        restarted = create_app(self.output)
        client = restarted.test_client()
        headers = {"X-RT-CSRF": restarted.config["RT_CSRF_TOKEN"]}
        restored = client.get("/api/batches/current", headers=headers).get_json()["batch"]
        self.assertEqual(restored["result"]["conversion_status"], "error")
        self.assertEqual(restored["result"]["conversion_error"], batch["result"]["conversion_error"])

    def test_cancelled_batch_automatically_exports_only_previously_successful_accounts(self):
        async def partial(email, **kwargs):
            if email.startswith("waiting"):
                await kwargs["otp_fetcher"](email)
            return bundle(email)

        self.app.extensions["rt_jobs"].extractor = partial
        response = self.client.post("/api/batches", headers=self.headers, json={
            "accounts_text": "\n".join(f"{email}----{PASSWORD}----{SECRET}" for email in (EMAIL, "waiting@example.com", "last@example.com")),
            "concurrency": 1,
        })
        batch_id = response.get_json()["id"]
        self.wait(f"/api/batches/{batch_id}", {"awaiting_code"})
        self.client.post(f"/api/batches/{batch_id}/cancel", headers=self.headers, json={})
        batch = self.wait(f"/api/batches/{batch_id}", {"cancelled"})
        self.assertEqual(batch["counts"]["success"], 1)
        self.assertEqual(batch["result"]["conversion_status"], "ready")
        with zipfile.ZipFile(batch["result"]["cpa_path"]) as z:
            self.assertEqual([json.loads(z.read(name))["email"] for name in z.namelist()], [EMAIL])
        document = json.loads(Path(batch["result"]["sub2api_path"]).read_text())
        self.assertEqual([account["credentials"]["email"] for account in document["accounts"]], [EMAIL])

    def test_conversion_source_preserves_full_metadata_and_excludes_failed_accounts(self):
        async def mixed(email, **_kwargs):
            if email.startswith("failed"):
                raise RuntimeError("synthetic extraction failure")
            return bundle(email)

        self.app.extensions["rt_jobs"].extractor = mixed
        emails = ("first@example.com", "failed@example.com", "last@example.com")
        batch = self.start_batch(*emails)
        self.assertEqual(batch["counts"]["success"], 2)
        endpoint = f"/api/batches/{batch['id']}/conversion-source"
        response = self.client.get(endpoint, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        expected = {"accounts": [build_cpa_record(bundle(email).to_dict()) for email in (emails[0], emails[2])]}
        self.assertEqual(response.get_json(), expected)
        for record in response.get_json()["accounts"]:
            self.assertEqual(record["plan_type"], "plus")
            self.assertEqual(record["expired"], bundle().expires_at)
            self.assertEqual(record["last_refresh"], bundle().obtained_at)
            self.assertEqual(record["account_id"], "synthetic-account")
        text = response.get_data(as_text=True)
        for private in (PASSWORD, SECRET, emails[1], "password", "totp_secret", "proxy"):
            self.assertNotIn(private, text)
        self.assertEqual(self.client.get(endpoint).status_code, 403)
        self.assertEqual(self.client.get(endpoint, headers={**self.headers, "Origin": "https://untrusted.example"}).status_code, 403)
        self.assertEqual(self.client.get(endpoint, headers=self.headers, environ_overrides={"REMOTE_ADDR": "203.0.113.20"}).status_code, 403)
        self.assertEqual(self.client.get("/api/batches/missing/conversion-source", headers=self.headers).status_code, 404)

        restarted = create_app(self.output)
        client = restarted.test_client()
        headers = {"X-RT-CSRF": restarted.config["RT_CSRF_TOKEN"]}
        self.assertEqual(client.get(endpoint, headers=headers).get_json(), expected)

    def test_conversion_source_refuses_incomplete_or_missing_success_files_atomically(self):
        batch = self.start_batch("first@example.com", "last@example.com")
        endpoint = f"/api/batches/{batch['id']}/conversion-source"
        path = Path(batch["rows"][1]["result"]["path"])
        original = path.read_text()
        invalid_records = ["{bad json", json.dumps({"refresh_token": "synthetic-private"}),
                           json.dumps({**bundle("last@example.com").to_dict(), "email": ""})]
        for invalid in invalid_records:
            with self.subTest(invalid=invalid[:30]):
                path.write_text(invalid)
                response = self.client.get(endpoint, headers=self.headers)
                self.assertEqual(response.status_code, 409)
                self.assertNotIn("accounts", response.get_json())
                self.assertNotIn("synthetic-private", response.get_data(as_text=True))
        path.write_text(original)
        path.unlink()
        self.assertEqual(self.client.get(endpoint, headers=self.headers).status_code, 409)

    def test_conversion_source_rejects_running_or_cancelled_batches_without_success(self):
        async def awaiting(email, **kwargs):
            await kwargs["otp_fetcher"](email)
            return bundle(email)

        self.app.extensions["rt_jobs"].extractor = awaiting
        response = self.client.post("/api/batches", headers=self.headers, json={
            "accounts_text": f"{EMAIL}----{PASSWORD}----{SECRET}", "concurrency": 1,
        })
        batch_id = response.get_json()["id"]
        self.wait(f"/api/batches/{batch_id}", {"awaiting_code"})
        endpoint = f"/api/batches/{batch_id}/conversion-source"
        try:
            self.assertEqual(self.client.get(endpoint, headers=self.headers).status_code, 409)
        finally:
            self.client.post(f"/api/batches/{batch_id}/cancel", headers=self.headers, json={})
            self.wait(f"/api/batches/{batch_id}", {"cancelled"})
        self.assertEqual(self.client.get(endpoint, headers=self.headers).status_code, 409)


if __name__ == "__main__":
    unittest.main()
