"""Automatic CPA/Sub2API output lifecycle using synthetic extraction only."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from rt_extractor.api import TokenBundle
from rt_extractor.cpa import build_cpa_record, save_export
from rt_extractor.sub2api import build_sub2api_export
from rt_extractor.web import create_app
from tests.web_client import browser_headers


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
        self.headers = browser_headers(self.app, self.client)

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

    def test_web_handoff_contains_all_formats_without_publishing_files(self):
        job = self.start_single()
        self.assertEqual(job["status"], "success")
        self.assert_public(job)
        record = self.client.get(
            f"/api/jobs/{job['id']}/result", headers=self.headers
        ).get_json()["records"][0]
        self.assertEqual(record["raw"], bundle().to_dict())
        self.assertEqual(record["cpa"], build_cpa_record(record["raw"]))
        self.assertEqual(
            record["sub2api"]["accounts"],
            build_sub2api_export([record["raw"]])["accounts"],
        )
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertNotIn("path", job["result"])
        for suffix in ("download", "rt", "cpa-download", "sub2api-download"):
            response = self.client.get(
                f"/api/jobs/{job['id']}/{suffix}", headers=self.headers
            )
            self.assertEqual(response.status_code, 410)
            self.assertNotIn("synthetic-rt", response.get_data(as_text=True))

    def test_batch_handoff_preserves_input_order_and_does_not_save_manifest(self):
        batch = self.start_batch("first@example.com", "last@example.com")
        self.assertEqual(batch["counts"]["success"], 2)
        self.assert_public(batch)
        handoff = self.client.get(
            f"/api/batches/{batch['id']}/results", headers=self.headers
        ).get_json()
        self.assertEqual(
            [row["email"] for row in handoff["records"]],
            ["first@example.com", "last@example.com"],
        )
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertNotIn("directory", batch["result"])
        response = self.client.post(
            f"/api/batches/{batch['id']}/ack",
            headers=self.headers,
            json={"ids": [row["id"] for row in handoff["records"]], "complete": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(
            self.client.get("/api/batches/current", headers=self.headers).get_json()[
                "batch"
            ]
        )
        self.assertEqual(list(self.output.iterdir()), [])

    def test_conversion_failure_does_not_leave_a_partially_deliverable_token(self):
        with patch(
            "rt_extractor.web_jobs.build_cpa_record",
            side_effect=ValueError("synthetic conversion error"),
        ):
            job = self.start_single()
        self.assertEqual(job["status"], "error")
        self.assertIsNone(job["result"])
        self.assert_public(job)
        self.assertEqual(list(self.output.iterdir()), [])
        response = self.client.get(
            f"/api/jobs/{job['id']}/result", headers=self.headers
        )
        self.assertIn(response.status_code, (200, 409))
        self.assertNotIn("synthetic-rt", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
