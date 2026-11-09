"""Local grouped extraction contracts; all credentials and network work are synthetic."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from rt_extractor.api import ExtractionError, TokenBundle
from rt_extractor.batch import BatchInputError, BatchManager
from rt_extractor.batch_cli import main, parse_grouped_accounts, run_groups


SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "synthetic-local-batch-password"


def line(number):
    return f"synthetic-{number}@example.com----{PASSWORD}----{SECRET}"


def bundle(email):
    return TokenBundle(email=email, access_token="synthetic-at", refresh_token="synthetic-rt",
                       id_token="synthetic-id", expires_in=3600,
                       obtained_at="2026-11-09T00:00:00+00:00", expires_at="2026-11-09T01:00:00+00:00")


class GroupedCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "results"
        self.messages = []

    def tearDown(self):
        self.temporary.cleanup()

    def run_grouped(self, groups, extractor, **kwargs):
        return run_groups(groups, self.output, extractor=extractor, poll_interval=.005,
                          report=self.messages.append, **kwargs)

    def assert_no_credentials(self, text):
        for secret in (PASSWORD, SECRET, "synthetic-at", "synthetic-rt", "synthetic-id", "@example.com"):
            self.assertNotIn(secret, text)

    def test_1001_accounts_run_as_ordered_groups_and_keep_original_lines(self):
        text = "\ufeff\n" + "\n".join(line(number) for number in range(1, 1002))
        groups = parse_grouped_accounts(text, concurrency=10)
        self.assertEqual([len(group) for group in groups], [1000, 1])
        self.assertEqual(groups[0][0][0], 2)
        self.assertEqual(groups[1][0][0], 1002)
        calls = []
        lock = threading.Lock()

        async def synthetic(email, **_kwargs):
            with lock:
                calls.append(email)
            if email != "synthetic-1001@example.com":
                raise ExtractionError("合成账号失败")
            return bundle(email)

        result = self.run_grouped(groups, synthetic, concurrency=10)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"]["total"], 1001)
        self.assertEqual(result["counts"]["error"], 1000)
        self.assertEqual(result["counts"]["success"], 1)
        self.assertEqual(result["concurrency"], 10)
        self.assertEqual(calls[-1], "synthetic-1001@example.com")
        self.assertEqual(len(set(calls[:-1])), 1000)
        self.assertTrue(all(not group for group in groups))
        for item in result["groups"]:
            state = json.loads((self.output / item["directory"] / "batch-state.json").read_text())
            self.assertEqual(state["concurrency"], 10)
            self.assertEqual(state["rows"][0]["line"], item["first_line"])
        self.assertEqual(set(result["groups"][1]["exports"]), {"raw_zip", "rt_text", "cpa_zip", "sub2api_json"})
        for path in result["groups"][1]["exports"].values():
            self.assertTrue((self.output / path).is_file())
        self.assert_no_credentials("\n".join(self.messages))
        self.assert_no_credentials((self.output / "progress.json").read_text())
        self.assert_no_credentials((self.output / "report.json").read_text())
        if os.name == "posix":
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
            for path in self.output.rglob("*"):
                if path.is_file():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_late_invalid_group_and_cross_group_duplicate_fail_before_login(self):
        first = "\n".join(line(number) for number in range(1000))
        for tail in ("invalid", line(0).replace("synthetic-0", "SYNTHETIC-0")):
            with self.subTest(tail_kind="format" if tail == "invalid" else "duplicate"):
                with self.assertRaises(BatchInputError) as raised:
                    parse_grouped_accounts(first + "\n\n" + tail, concurrency=10)
                self.assertEqual(raised.exception.errors[-1]["line"], 1002)
                self.assert_no_credentials(json.dumps(raised.exception.errors))
        input_file = self.root / "input.txt"
        input_file.write_text(first + "\ninvalid")
        with patch("rt_extractor.batch_cli.run_groups") as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--input", str(input_file), "--output-dir", str(self.output), "--concurrency", "10"]), 2)
        run.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assert_no_credentials(output.getvalue())

    def test_email_skip_saves_other_results_and_never_logs_tokens(self):
        groups = parse_grouped_accounts("\n".join([line(1), line(2)]), skip_email_verification=True)

        async def synthetic(email, **kwargs):
            if email.startswith("synthetic-1@"):
                await kwargs["otp_fetcher"](email)
            return bundle(email)

        result = self.run_grouped(groups, synthetic, skip_email_verification=True)
        self.assertEqual(result["counts"]["success"], 1)
        self.assertEqual(result["counts"]["skipped"], 1)
        raw_files = list(self.output.glob("batch-*/tokens-*.json"))
        self.assertEqual(len(raw_files), 1)
        self.assertEqual(json.loads(raw_files[0].read_text())["email"], "synthetic-2@example.com")
        self.assert_no_credentials("\n".join(self.messages))

    def test_cancel_does_not_start_next_group_and_clears_pending_credentials(self):
        groups = parse_grouped_accounts("\n".join(line(number) for number in range(1001)))
        credentials = [values for group in groups for _, values in group]
        stop = threading.Event()
        calls = []

        async def synthetic(email, **_kwargs):
            calls.append(email)
            stop.set()
            while True:
                await asyncio.sleep(.005)

        result = self.run_grouped(groups, synthetic, concurrency=1, stop_event=stop)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["counts"]["cancelled"], 1001)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["groups"][1]["status"], "cancelled")
        self.assertNotIn("id", result["groups"][1])
        self.assertTrue(all(not values for values in credentials))
        self.assertEqual(len(list(self.output.glob("batch-*"))), 1)

    def test_uncooperative_cleanup_has_a_bounded_report_and_no_next_group(self):
        groups = parse_grouped_accounts(line(1) + "\n" + line(2))
        groups = [[groups[0][0]], [groups[0][1]]]
        stop, release, finished = threading.Event(), threading.Event(), threading.Event()
        manager_finished = threading.Event()
        manager_threads = []

        class TrackedBatchManager(BatchManager):
            def _run(self, *args):
                manager_threads.append(threading.current_thread())
                try:
                    return super()._run(*args)
                finally:
                    manager_finished.set()

        async def synthetic(**_kwargs):
            stop.set()
            try:
                while True:
                    await asyncio.sleep(.005)
            except asyncio.CancelledError:
                try:
                    while not release.is_set():
                        await asyncio.sleep(.005)
                finally:
                    finished.set()
                raise

        with patch("rt_extractor.batch_cli.BatchManager", TrackedBatchManager):
            try:
                result = self.run_grouped(groups, synthetic, concurrency=1, stop_event=stop, cancel_grace=.01)
                self.assertEqual(result["status"], "cleanup_timeout")
                self.assertNotIn("id", result["groups"][1])
                self.assertEqual(json.loads((self.output / "report.json").read_text())["status"], "cleanup_timeout")
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
                # The extractor's finally runs before the batch scheduler joins
                # its worker and writes terminal metadata. Wait for that entire
                # scheduler thread before TemporaryDirectory removes its output.
                self.assertTrue(manager_finished.wait(2))
                self.assertEqual(len(manager_threads), 1)
                manager_threads[0].join(1)
                self.assertFalse(manager_threads[0].is_alive())

    def test_existing_directory_is_never_reused(self):
        self.output.mkdir()
        sentinel = self.output / "existing.txt"
        sentinel.write_text("keep")

        async def forbidden(**_kwargs):
            self.fail("must not log in")

        with self.assertRaisesRegex(ValueError, "全新的目录"):
            self.run_grouped(parse_grouped_accounts(line(1)), forbidden)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_main_returns_failure_when_accounts_succeed_but_group_export_fails(self):
        input_file = self.root / "input.txt"
        input_file.write_text(line(1))
        report = {"status": "completed", "counts": {"success": 1, "error": 0},
                  "groups": [{"conversion_status": "error"}]}
        with patch("rt_extractor.batch_cli.run_groups", return_value=report) as run:
            code = main(["--input", str(input_file), "--output-dir", str(self.output)])
        self.assertEqual(code, 1)
        run.assert_called_once()
        self.assertEqual(report["counts"]["success"], 1)


if __name__ == "__main__":
    unittest.main()
