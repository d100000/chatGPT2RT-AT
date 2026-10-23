"""Bounded batch scheduling contracts, with synthetic accounts and explicit gates."""

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
from tests.web_client import browser_headers, workspace_for, new_browser
from rt_extractor.abuse import AbusePolicy, ClientPolicy, Quota


SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD_PREFIX = "synthetic-concurrent-password-"
OMITTED = object()


def password(email: str) -> str:
    return PASSWORD_PREFIX + email.partition("@")[0]


def account_line(email: str) -> str:
    return f"{email}----{password(email)}----{SECRET}"


def bundle(email: str) -> TokenBundle:
    local = email.partition("@")[0]
    return TokenBundle(
        email=email, access_token=f"synthetic-at-{local}",
        refresh_token=f"synthetic-rt-{local}", id_token=f"synthetic-id-{local}",
        chatgpt_account_id=f"synthetic-account-{local}",
        chatgpt_user_id=f"synthetic-user-{local}",
        expires_in=3600, obtained_at="2026-10-22T01:00:00+00:00",
        expires_at="2026-10-22T02:00:00+00:00",
    )


async def wait_gate(gate: threading.Event) -> None:
    # Async polling lets each test cancel independently of its release gate.
    while not gate.is_set():
        await asyncio.sleep(0.005)


class ConcurrentBatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.lock = threading.Lock()
        self.calls = []
        self.active = 0
        self.peak = 0
        self.batch_ids = []
        self.gates = []

        async def successful(email, _password, **_kwargs):
            return bundle(email)

        self.behavior = successful

        async def dispatch(email, password, **kwargs):
            with self.lock:
                self.calls.append((email, password, kwargs.get("totp_secret")))
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                return await self.behavior(email, password, **kwargs)
            finally:
                with self.lock:
                    self.active -= 1

        # This scheduler harness polls every 10 ms and validates eight invalid
        # submissions in one window. Keep limits on with explicit test budgets.
        self.app = create_app(
            self.output,
            extractor=dispatch,
            abuse_policy=AbusePolicy(
                burst=Quota(1000, 1),
                global_burst=Quota(1000, 1),
                device=ClientPolicy(burst=Quota(1000, 1), submissions=Quota(10, 60)),
                session=ClientPolicy(burst=Quota(1000, 1), submissions=Quota(10, 60)),
            ),
        )
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.headers = browser_headers(self.app, self.client)

    def tearDown(self):
        # Release even failed tests before waiting for their worker threads.
        for gate in self.gates:
            gate.set()
        for batch_id in self.batch_ids:
            response = self.client.get(f"/api/batches/{batch_id}", headers=self.headers)
            if response.status_code == 404:
                # The manager intentionally retains the latest three batches.
                continue
            if response.status_code == 200 and response.get_json()["status"] not in {"completed", "cancelled"}:
                self.client.post(f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers)
            self.wait(batch_id, lambda state: state["status"] in {"completed", "cancelled"})
        self.directory.cleanup()

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def start(self, emails, *, concurrency=OMITTED):
        payload = {"accounts_text": "\n".join(account_line(email) for email in emails)}
        if concurrency is not OMITTED:
            payload["concurrency"] = concurrency
        response = self.client.post("/api/batches", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        snapshot = response.get_json()
        self.batch_ids.append(snapshot["id"])
        return snapshot["id"]

    def wait(self, batch_id, predicate):
        deadline = time.monotonic() + 5
        last = None
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/batches/{batch_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            last = response.get_json()
            if predicate(last):
                return last
            time.sleep(0.005)
        self.fail(f"Synthetic batch did not reach expected state; last status={last['status']}, counts={last['counts']}")

    def assert_redacted(self, state):
        text = json.dumps(state)
        for value in (PASSWORD_PREFIX, SECRET, "synthetic-at-", "synthetic-rt-", "synthetic-id-"):
            self.assertNotIn(value, text)

    def test_invalid_concurrency_rejects_before_starting_any_account(self):
        for value in (True, False, None, "2", 2.0, 0, -1, 4, [], {}):
            with self.subTest(value_type=type(value).__name__, value=str(value)):
                response = self.client.post(
                    "/api/batches", json={"accounts_text": account_line("valid@example.com"), "concurrency": value},
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.get_json().get("errors"))
                self.assert_redacted(response.get_json())
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.output.rglob("*.json")), [])

    def test_default_two_and_each_selected_limit_are_actually_bounded(self):
        for setting, expected in ((OMITTED, 2), (1, 1), (2, 2), (3, 3)):
            with self.subTest(concurrency=expected, omitted=setting is OMITTED):
                release = self.gate()
                with self.lock:
                    self.calls.clear()
                    self.peak = 0

                async def blocked(email, _password, **_kwargs):
                    await wait_gate(release)
                    return bundle(email)

                self.behavior = blocked
                emails = [f"person{i}@example.com" for i in range(6)]
                batch_id = self.start(emails, concurrency=setting)

                def at_limit(state):
                    with self.lock:
                        return (
                            state["counts"]["running"] == expected
                            and len(self.calls) == expected
                        )

                running = self.wait(batch_id, at_limit)
                self.assertEqual(running["concurrency"], expected)
                self.assertEqual(len(running["active_jobs"]), expected)
                self.assertEqual(running["counts"]["queued"], len(emails) - expected)
                self.assertEqual(running["counts"]["awaiting_code"], 0)
                with self.lock:
                    self.assertEqual(len(self.calls), expected)
                    self.assertEqual(self.peak, expected)
                self.assert_redacted(running)
                release.set()
                finished = self.wait(
                    batch_id, lambda state: state["status"] == "completed"
                )
                self.assertEqual(finished["counts"]["success"], len(emails))
                self.assertEqual(finished["active_jobs"], [])
                with self.lock:
                    self.assertLessEqual(self.peak, expected)
                    self.assertEqual(self.active, 0)
                    self.assertEqual(
                        sorted(self.calls),
                        sorted((email, password(email), SECRET) for email in emails),
                    )
                records = self.client.get(
                    f"/api/batches/{batch_id}/results", headers=self.headers
                ).get_json()["records"]
                response = self.client.post(
                    f"/api/batches/{batch_id}/ack",
                    json={
                        "ids": [record["id"] for record in records],
                        "complete": True,
                    },
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 200)

    def test_free_slot_advances_past_failure_while_slow_account_is_running_and_export_stays_ordered(
        self,
    ):
        release = self.gate()
        emails = [
            "slow@example.com",
            "failed@example.com",
            "fast@example.com",
            "last@example.com",
        ]

        async def mixed(email, _password, **_kwargs):
            if email.startswith("slow"):
                await wait_gate(release)
            if email.startswith("failed"):
                raise RuntimeError(f"must be redacted: {PASSWORD_PREFIX} {SECRET}")
            return bundle(email)

        self.behavior = mixed
        batch_id = self.start(emails, concurrency=2)
        partial = self.wait(
            batch_id,
            lambda state: (
                state["counts"]["success"] == 2 and state["counts"]["error"] == 1
            ),
        )
        self.assertFalse(release.is_set())
        self.assertEqual(partial["status"], "running")
        self.assertEqual(partial["counts"]["queued"], 0)
        self.assertEqual([job["email"] for job in partial["active_jobs"]], [emails[0]])
        self.assertEqual([row["email"] for row in partial["rows"]], emails)
        self.assert_redacted(partial)
        release.set()
        finished = self.wait(batch_id, lambda state: state["status"] == "completed")
        successful = [emails[0], emails[2], emails[3]]
        self.assertEqual(finished["counts"]["success"], 3)
        response = self.client.get(
            f"/api/batches/{batch_id}/results", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        document = {
            "accounts": [
                record["sub2api"]["accounts"][0]
                for record in response.get_json()["records"]
            ]
        }
        response.close()
        self.assertEqual(
            [account["credentials"]["email"] for account in document["accounts"]],
            successful,
        )
        for account in document["accounts"]:
            source = bundle(account["credentials"]["email"])
            for field in (
                "access_token",
                "refresh_token",
                "id_token",
                "chatgpt_account_id",
            ):
                self.assertEqual(account["credentials"][field], getattr(source, field))
        for row in finished["rows"]:
            response = self.client.get(
                f"/api/jobs/{row['id']}/result", headers=self.headers
            )
            if row["status"] == "success":
                self.assertEqual(
                    response.get_json()["records"][0]["raw"]["refresh_token"],
                    bundle(row["email"]).refresh_token,
                )
            else:
                self.assertEqual(response.get_json()["records"], [])

    def test_two_challenges_are_isolated_and_another_slot_keeps_processing(self):
        third_release = self.gate()
        received = {}
        emails = ["first@example.com", "second@example.com", "third@example.com"]

        async def challenged(email, _password, **kwargs):
            if email in emails[:2]:
                code = await kwargs["otp_fetcher"](email)
                with self.lock:
                    received[email] = code
            else:
                await wait_gate(third_release)
            return bundle(email)

        self.behavior = challenged
        batch_id = self.start(emails, concurrency=2)
        waiting = self.wait(batch_id, lambda state: state["counts"]["awaiting_code"] == 2)
        self.assertEqual(waiting["status"], "awaiting_code")
        self.assertEqual(waiting["counts"]["running"], 0)
        self.assertEqual(waiting["counts"]["queued"], 1)
        first, second = waiting["active_jobs"]
        self.assertEqual([first["email"], second["email"]], emails[:2])
        self.assertNotEqual(first["challenge"]["id"], second["challenge"]["id"])
        wrong = self.client.post(
            f"/api/jobs/{first['id']}/code",
            json={"challenge_id": second["challenge"]["id"], "code": "111111"}, headers=self.headers,
        )
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(received, {})
        accepted = self.client.post(
            f"/api/jobs/{first['id']}/code",
            json={"challenge_id": first["challenge"]["id"], "code": "012345"}, headers=self.headers,
        )
        self.assertEqual(accepted.status_code, 200)
        mixed = self.wait(batch_id, lambda state: state["counts"]["success"] == 1 and state["counts"]["running"] == 1)
        self.assertEqual(mixed["status"], "running")
        self.assertEqual(mixed["counts"]["awaiting_code"], 1)
        self.assertEqual(mixed["counts"]["queued"], 0)
        self.assertEqual({job["email"] for job in mixed["active_jobs"]}, set(emails[1:]))
        accepted = self.client.post(
            f"/api/jobs/{second['id']}/code",
            json={"challenge_id": second["challenge"]["id"], "code": "654321"}, headers=self.headers,
        )
        self.assertEqual(accepted.status_code, 200)
        repeated = self.client.post(
            f"/api/jobs/{first['id']}/code",
            json={"challenge_id": first["challenge"]["id"], "code": "012345"}, headers=self.headers,
        )
        self.assertEqual(repeated.status_code, 409)
        third_release.set()
        finished = self.wait(batch_id, lambda state: state["status"] == "completed")
        self.assertEqual(finished["counts"]["success"], 3)
        self.assertEqual(received, {emails[0]: "012345", emails[1]: "654321"})
        for value in ("111111", "012345", "654321"):
            self.assertNotIn(value, json.dumps(finished))
        self.assert_redacted(finished)

    def test_cancel_stops_all_active_and_pending_accounts_and_waits_for_cleanup(self):
        cleanup_release = self.gate()
        cleaning = set()
        emails = [
            "saved@example.com",
            "busy1@example.com",
            "busy2@example.com",
            "queued@example.com",
        ]

        async def cancellable(email, _password, **_kwargs):
            if email == emails[0]:
                return bundle(email)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                with self.lock:
                    cleaning.add(email)
                await wait_gate(cleanup_release)
                raise

        self.behavior = cancellable
        batch_id = self.start(emails, concurrency=2)
        before = self.wait(
            batch_id,
            lambda state: (
                state["counts"]["success"] == 1 and state["counts"]["running"] == 2
            ),
        )
        response = self.client.post(
            f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "cancelling")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.lock:
                if cleaning == set(emails[1:3]):
                    break
            time.sleep(0.005)
        self.assertEqual(cleaning, set(emails[1:3]))
        # A second cancellation must not interrupt the first cancellation's cleanup.
        again = self.client.post(
            f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers
        )
        self.assertEqual(again.get_json()["status"], "cancelling")
        current = self.client.get(
            f"/api/batches/{batch_id}", headers=self.headers
        ).get_json()
        self.assertEqual(current["status"], "cancelling")
        denied = self.client.post(
            "/api/batches",
            json={"accounts_text": account_line("later@example.com")},
            headers=self.headers,
        )
        self.assertEqual(denied.status_code, 409)
        denied = self.client.post(
            "/api/jobs",
            json={"email": "later@example.com", "password": "synthetic"},
            headers=self.headers,
        )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(
            self.client.get(
                f"/api/batches/{batch_id}/results", headers=self.headers
            ).status_code,
            200,
        )
        saved_row = before["rows"][0]
        self.assertEqual(
            self.client.get(
                f"/api/jobs/{saved_row['id']}/result", headers=self.headers
            ).get_json()["records"][0]["raw"]["refresh_token"],
            bundle(emails[0]).refresh_token,
        )
        cleanup_release.set()
        finished = self.wait(batch_id, lambda state: state["status"] == "cancelled")
        self.assertEqual(finished["counts"]["success"], 1)
        self.assertEqual(finished["counts"]["cancelled"], 3)
        self.assertEqual(finished["counts"]["running"], 0)
        self.assertEqual(finished["counts"]["awaiting_code"], 0)
        self.assertEqual(finished["counts"]["queued"], 0)
        self.assertEqual(finished["active_jobs"], [])
        with self.lock:
            self.assertEqual({call[0] for call in self.calls}, set(emails[:3]))
            self.assertEqual(self.active, 0)
        response = self.client.get(
            f"/api/batches/{batch_id}/results", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [
                account["credentials"]["email"]
                for account in [
                    record["sub2api"]["accounts"][0]
                    for record in response.get_json()["records"]
                ]
            ],
            [emails[0]],
        )
        response.close()

    def test_cancel_waits_for_an_already_running_executor_before_releasing_batch(self):
        release = self.gate()
        entered = threading.Event()

        def blocking_work():
            entered.set()
            release.wait(5)

        async def blocked(email, _password, **_kwargs):
            await asyncio.get_running_loop().run_in_executor(None, blocking_work)
            return bundle(email)

        self.behavior = blocked
        batch_id = self.start(["vm@example.com", "queued@example.com"], concurrency=1)
        self.assertTrue(entered.wait(3))
        response = self.client.post(f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers)
        self.assertEqual(response.get_json()["status"], "cancelling")
        current = self.client.get(f"/api/batches/{batch_id}", headers=self.headers).get_json()
        self.assertEqual(current["status"], "cancelling")
        denied = self.client.post("/api/batches", json={"accounts_text": account_line("later@example.com")}, headers=self.headers)
        self.assertEqual(denied.status_code, 409)
        release.set()
        finished = self.wait(batch_id, lambda state: state["status"] == "cancelled")
        self.assertEqual(finished["counts"]["cancelled"], 2)
        self.assertEqual([call[0] for call in self.calls], ["vm@example.com"])

    def test_completed_concurrency_and_account_results_are_never_recovered_on_restart(
        self,
    ):
        emails = ["first@example.com", "second@example.com", "third@example.com"]
        batch_id = self.start(emails, concurrency=3)
        finished = self.wait(batch_id, lambda state: state["status"] == "completed")
        self.assertEqual(finished["counts"]["success"], 3)

        async def forbidden(**_kwargs):
            raise AssertionError("Recovery must not authenticate")

        restarted = create_app(self.output, extractor=forbidden)
        client, headers = new_browser(restarted)
        self.assertIsNone(
            client.get("/api/batches/current", headers=headers).get_json()["batch"]
        )
        self.assertEqual(
            client.get(f"/api/batches/{batch_id}/results", headers=headers).status_code,
            404,
        )
        for row in finished["rows"]:
            self.assertEqual(
                client.get(
                    f"/api/jobs/{row['id']}/result", headers=headers
                ).status_code,
                404,
            )
        self.assertEqual(list(self.output.iterdir()), [])

    def test_rate_limit_pauses_new_dispatch_but_other_account_can_continue(self):
        from rt_extractor._protocol.login import RateLimitedError

        trigger_limit = self.gate()
        running_release = self.gate()
        third_started = threading.Event()
        emails = ["limited@example.com", "running@example.com", "queued@example.com"]

        async def limited(email, _password, **_kwargs):
            if email == emails[0]:
                await wait_gate(trigger_limit)
                raise RateLimitedError(retry_after=1)
            if email == emails[1]:
                await wait_gate(running_release)
            else:
                third_started.set()
            return bundle(email)

        self.behavior = limited
        batch_id = self.start(emails, concurrency=2)
        self.wait(batch_id, lambda state: state["counts"]["running"] == 2)
        trigger_limit.set()
        paused = self.wait(batch_id, lambda state: state.get("cooldown_seconds", 0) > 0 and state["counts"]["error"] == 1)
        self.assertEqual(paused["status"], "running")
        self.assertEqual(paused["counts"]["queued"], 1)
        self.assertFalse(third_started.is_set())
        # An already running account can finish even while new dispatch is paused.
        running_release.set()
        still_paused = self.wait(batch_id, lambda state: state["counts"]["success"] >= 1)
        self.assertEqual(still_paused["rows"][1]["status"], "success")
        finished = self.wait(batch_id, lambda state: state["status"] == "completed")
        self.assertTrue(third_started.is_set())
        self.assertEqual(finished["counts"]["success"], 2)
        self.assertEqual(finished["counts"]["error"], 1)
        with self.lock:
            self.assertEqual(sum(call[0] == emails[0] for call in self.calls), 1)
        self.assert_redacted(finished)

    def test_cancel_interrupts_long_rate_limit_wait_without_starting_queued_accounts(self):
        from rt_extractor._protocol.login import RateLimitedError

        async def limited(_email, _password, **_kwargs):
            raise RateLimitedError(retry_after=60)

        self.behavior = limited
        emails = ["limited@example.com", "queued1@example.com", "queued2@example.com"]
        batch_id = self.start(emails, concurrency=1)
        self.wait(batch_id, lambda state: state.get("cooldown_seconds", 0) > 0 and state["counts"]["error"] == 1)
        response = self.client.post(f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        finished = self.wait(batch_id, lambda state: state["status"] == "cancelled")
        self.assertEqual(finished["counts"]["error"], 1)
        self.assertEqual(finished["counts"]["cancelled"], 2)
        self.assertEqual(finished["active_jobs"], [])
        self.assertEqual([call[0] for call in self.calls], [emails[0]])

    def test_rate_limit_is_published_before_the_limited_worker_returns_to_dispatch(
        self,
    ):
        from rt_extractor._protocol.login import RateLimitedError

        trigger_limit = self.gate()
        running_release = self.gate()
        limited_consume_release = self.gate()
        limited_returned = threading.Event()
        emails = ["limited@example.com", "running@example.com", "queued@example.com"]
        jobs = workspace_for(self.app, self.client).jobs
        original_run = jobs._run

        def delayed_return(job, credentials, **kwargs):
            original_run(job, credentials, **kwargs)
            if job.email == emails[0]:
                # Hold this worker before BatchManager._consume's finally block.
                # The other worker must already see the shared dispatch pause.
                limited_returned.set()
                limited_consume_release.wait(5)

        jobs._run = delayed_return
        self.addCleanup(setattr, jobs, "_run", original_run)

        async def limited(email, _password, **_kwargs):
            if email == emails[0]:
                await wait_gate(trigger_limit)
                raise RateLimitedError(retry_after=60)
            if email == emails[1]:
                await wait_gate(running_release)
            return bundle(email)

        self.behavior = limited
        batch_id = self.start(emails, concurrency=2)
        self.wait(batch_id, lambda state: state["counts"]["running"] == 2)
        trigger_limit.set()
        self.assertTrue(limited_returned.wait(3))
        paused = self.client.get(
            f"/api/batches/{batch_id}", headers=self.headers
        ).get_json()
        self.assertGreater(paused["cooldown_seconds"], 0)
        self.assertEqual(paused["counts"]["queued"], 1)
        running_release.set()
        other_finished = self.wait(
            batch_id, lambda state: state["rows"][1]["status"] == "success"
        )
        self.assertFalse(limited_consume_release.is_set())
        self.assertGreater(other_finished["cooldown_seconds"], 0)
        self.assertEqual(other_finished["counts"]["success"], 1)
        self.assertEqual(other_finished["rows"][2]["status"], "queued")
        with self.lock:
            self.assertEqual({call[0] for call in self.calls}, set(emails[:2]))
        self.client.post(
            f"/api/batches/{batch_id}/cancel", json={}, headers=self.headers
        )
        limited_consume_release.set()
        finished = self.wait(batch_id, lambda state: state["status"] == "cancelled")
        self.assertEqual(finished["counts"]["error"], 1)
        self.assertEqual(finished["counts"]["success"], 1)
        self.assertEqual(finished["counts"]["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
