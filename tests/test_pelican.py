import asyncio
import base64
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

from rt_extractor.eval_catalog import catalog
from rt_extractor.eval_protocol import body_for, endpoint, generate, models_endpoint, redact
from rt_extractor.eval_reports import csv_export, html_export, review_summary
from rt_extractor.eval_svg import CHECKER_VERSION, extract_svgs, inspect_svg, svg_hash, validate_png
from rt_extractor.pelican import ACTIVE, COOKIE, HistoryError, PelicanService, configuration, plan_configuration, round_requests
from rt_extractor.web import create_app
from tests.pelican_mock import SVG, mock_provider
from tests.web_client import browser_headers

KEY = "mock-private-key-ONLY-FOR-TESTS"


def wait_for(check, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.015)
    raise AssertionError("Timed out waiting for test state")


def png_fixture():
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value) & 0xffffffff)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff\xff")) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(data).decode()


def review(score, reviewer="one"):
    return {"reviewer": reviewer, "dimensions": {k: {"score": score, "reason": "test observation", "location": "center"} for k in "ABCD"}}


class ConfigAndSvgTests(unittest.TestCase):
    def test_original_prompt_and_parameters_without_tools_system_or_repairs(self):
        q = catalog()[0]
        self.assertEqual(q["prompt"], "Generate an SVG of a pelican riding a bicycle")
        self.assertFalse(q["official"])
        for protocol in ["responses", "chat"]:
            config, _ = configuration({"base_url": "https://example.test", "api_key": KEY, "protocol": protocol})
            body = body_for(config, "gpt-6.1-sol", q["prompt"])
            self.assertEqual(body["model"], "gpt-6.1-sol")
            messages = body.get("messages", body.get("input"))
            self.assertEqual(messages, [{"role": "user", "content": q["prompt"]}])
            self.assertNotIn("temperature", body)
            self.assertNotIn("seed", body)
            self.assertNotIn("tools", body)
            self.assertEqual(config["timeout_seconds"], 600)
            self.assertEqual(config["max_output_tokens"], 16000)
            if protocol == "responses": self.assertIs(body["store"], False)

    def test_endpoint_normalization_preserves_proxy_prefix_and_switches_full_endpoint(self):
        for base in ["https://example.test/prefix/v1/", "https://example.test/prefix/v1/responses", "https://example.test/prefix/v1/chat/completions"]:
            self.assertEqual(endpoint(base, "responses"), "https://example.test/prefix/v1/responses")
            self.assertEqual(endpoint(base, "chat"), "https://example.test/prefix/v1/chat/completions")
            self.assertEqual(models_endpoint(base, "chat"), "https://example.test/prefix/v1/models")
        self.assertEqual(endpoint("http://localhost:8888", "responses"), "http://localhost:8888/v1/responses")
        for bad in ["file:///tmp/test", "https://user:key@example.test", "https://example.test/?key=secret", "javascript:alert(1)"]:
            with self.assertRaises(ValueError): endpoint(bad, "responses")

    def test_defaults_simple_batch_and_continuous_ignores_limits(self):
        config, _ = plan_configuration({"base_url": "https://example.test", "api_key": KEY})
        self.assertEqual(config["models"], ["gpt-6.1-sol"])
        self.assertEqual(config["requests_per_round"], 1)
        self.assertIs(config["random_questions"], True)
        self.assertEqual(len(config["questions"]), 100)
        self.assertTrue(all(q["animation"] for q in config["questions"]))
        batch, _ = plan_configuration({"base_url": "https://example.test", "api_key": KEY, "mode": "batch", "models": ["a", "b"]})
        self.assertEqual(batch["requests_per_round"], 6)
        loop, _ = plan_configuration({"base_url": "https://example.test", "api_key": KEY, "mode": "continuous", "interval_seconds": 999, "max_rounds": 1})
        self.assertEqual((loop["interval_seconds"], loop["max_rounds"]), (0, 0))
        with self.assertRaises(ValueError): plan_configuration({"base_url": "https://example.test", "api_key": KEY, "models": ["a", "b"]})

    def test_random_switch_is_strict_and_fixed_baseline_remains_available(self):
        base = {"base_url": "https://example.test", "api_key": KEY}
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                plan_configuration(base | {"random_questions": value})
        fixed, _ = plan_configuration(base | {"random_questions": False})
        self.assertEqual([q["id"] for q in fixed["questions"]], ["pelican-bicycle"])
        self.assertEqual(fixed["requests_per_round"], 1)
        random, _ = plan_configuration(base | {"questions": ["stale-id"]})
        self.assertEqual(len(random["questions"]), 100)
        with self.assertRaises(ValueError):
            plan_configuration(base | {"random_questions": False, "questions": ["stale-id"]})

    def test_random_draws_share_each_prompt_across_models_and_exhaust_pool_across_rounds(self):
        config, _ = plan_configuration({"base_url": "https://example.test", "api_key": KEY,
                                       "mode": "batch", "models": ["one", "two"], "repeats": 7})
        self.assertEqual(config["requests_per_round"], 14)
        bag, draws = [], []
        for _ in range(15):
            requests = list(round_requests(config, bag))
            self.assertEqual(len(requests), 14)
            for index in range(0, len(requests), 2):
                one, two = requests[index:index + 2]
                self.assertEqual(one[0], two[0])
                self.assertEqual((one[1], two[1]), ("one", "two"))
                self.assertEqual(one[2], two[2])
                draws.append(one[0]["id"])
        self.assertEqual(len(set(draws[:100])), 100)
        self.assertEqual(len(set(draws[100:])), 5)
        self.assertTrue(set(draws[100:]) <= set(draws[:100]))

    def test_legacy_plans_without_random_flag_keep_fixed_order(self):
        config = {"questions": [{"id": "a"}, {"id": "b"}], "models": ["one", "two"], "repeats": 2}
        rows = [(q["id"], model, repetition) for q, model, repetition in round_requests(config, [])]
        self.assertEqual(rows, [(q, m, r) for q in ("a", "b") for m in ("one", "two") for r in (1, 2)])

    def test_svg_extraction_is_first_complete_and_never_repaired(self):
        raw = f"```svg\n{SVG}\n```\n<svg><g></svg>"
        svg, checks = inspect_svg(raw)
        self.assertEqual(svg, SVG)
        self.assertEqual(checks["svg_count"], 2)
        self.assertTrue(checks["safe"])
        self.assertIsNone(checks["browser_rendered"])
        self.assertEqual(extract_svgs('<svg><svg /></svg>'), ['<svg><svg /></svg>'])
        self.assertEqual(extract_svgs('<!-- <svg/> --><svg/>'), ['<svg/>'])
        self.assertFalse(inspect_svg('<svg><g></svg>')[1]["structure_valid"])
        self.assertFalse(inspect_svg('<svg><g>')[1]["complete_svg"])

    def test_security_rejects_executable_external_css_and_entities(self):
        bad = ['<script>alert(1)</script>', '<foreignObject><div>hi</div></foreignObject>', '<image href="https://evil.test/x"/>', '<use href="data:image/svg+xml,bad"/>', '<g onload="evil()"/>', '<style>@import "https://evil.test/a";</style>', '<path fill="url(https://evil.test/a)"/>', '<g style="fill:u\\72l(https://evil.test)"/>', '<a href="#id">x</a>']
        for item in bad:
            with self.subTest(item=item): self.assertFalse(inspect_svg(f'<svg xmlns="http://www.w3.org/2000/svg">{item}</svg>')[1]["safe"])
        self.assertFalse(inspect_svg('<!DOCTYPE svg [<!ENTITY a SYSTEM "file:///etc/passwd">]><svg>&a;</svg>')[1]["safe"])
        self.assertTrue(inspect_svg('<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="a"><stop offset="0"/></linearGradient></defs><path fill="url(#a)"/></svg>')[1]["safe"])

    def test_png_evidence_is_validated(self):
        self.assertEqual(validate_png(png_fixture())["width"], 1)
        for value in ["data:image/svg+xml,<svg/>", "data:image/png;base64,AAAA", png_fixture()[:-6] + "AAAAAA"]:
            with self.assertRaises(ValueError): validate_png(value)

    def test_redaction_handles_exact_json_url_and_bearer(self):
        result = redact({"raw": KEY + " Bearer abcdef sk-abcdefghijk", "api_key": "unrelated"}, (KEY,))
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn("abcdef", result["raw"])
        self.assertEqual(result["api_key"], "[REDACTED]")

    def test_secret_fragments_cannot_be_reassembled_from_exported_sse(self):
        for field in ("delta", "content"):
            chunks = [KEY[:11], KEY[11:22], KEY[22:], SVG]
            wire = ''.join('data: ' + json.dumps({field: c}) + '\n\n' for c in chunks)
            redacted = redact(wire, (KEY,))
            assembled = ''.join(json.loads(line[6:])[field] for line in redacted.splitlines() if line.startswith('data: '))
            self.assertNotIn(KEY, assembled)
            self.assertIn(SVG, assembled)
        partial = 'data: ' + json.dumps({"delta": KEY[:12]}) + '\n\n'
        self.assertNotIn(KEY[:12], redact(partial, (KEY,)))


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = mock_provider()
        cls.server, cls.url = cls.provider.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.provider.__exit__(None, None, None)

    def generate(self, model, protocol="responses", timeout=3):
        config, _ = configuration({"base_url": self.url, "api_key": KEY, "protocol": protocol, "timeout_seconds": timeout})
        output = {}
        asyncio.run(generate(config, KEY, body_for(config, model, catalog()[0]["prompt"]), output, lambda _: None))
        return output

    def test_both_protocols_stream_and_json_fallback_ttft_and_zero_usage(self):
        for protocol in ("responses", "chat"):
            for model in ("mock-good", "mock-json"):
                with self.subTest(protocol=protocol, model=model):
                    r = self.generate(model, protocol)
                    self.assertEqual(r["status"], "success")
                    self.assertEqual(r["raw_output"], SVG)
                    self.assertEqual(r["returned_model"], "mock-returned")
                    self.assertEqual(r["usage"]["input"], 0)
                    if model == "mock-json": self.assertIsNone(r["ttft_ms"])
                    else: self.assertGreaterEqual(r["ttft_ms"], 30)

    def test_missing_usage_is_unknown_not_zero(self):
        self.assertEqual(self.generate("mock-no-usage")["usage"], {"input": None, "output": None, "total": None})

    def test_http_error_stream_disconnect_length_and_total_timeout_keep_evidence(self):
        for model in ["mock-error", "mock-disconnect", "mock-length", "mock-timeout"]:
            with self.subTest(model=model):
                r = self.generate(model, timeout=1)
                self.assertEqual(r["status"], "timeout" if model == "mock-timeout" else "failed")
                self.assertTrue(r["raw_wire"])
                if model != "mock-error": self.assertTrue(r["raw_output"])


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = mock_provider()
        cls.server, cls.url = cls.provider.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.provider.__exit__(None, None, None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PelicanService(self.temp.name)
        self.owner = "test-owner"
        wait_for(lambda: self.server.active == 0)
        with self.server.lock:
            self.server.requests.clear()
            self.server.max_active = 0

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def start(self, **options):
        return self.service.start(self.owner, {"base_url": self.url, "api_key": KEY, "models": ["mock-good"], "random_questions": False, **options})

    def finish(self, batch):
        return wait_for(lambda: next((b for b in self.service.tasks(self.owner) if b["id"] == batch["id"] and b["status"] not in ACTIVE), None))

    def ready_sample(self):
        self.finish(self.start())
        r = self.service.samples(self.owner)[0]
        return self.service.render(self.owner, r["id"], {"success": True, "svg_sha256": svg_hash(r["svg"]), "png": png_fixture()})

    def test_batch_serial_failures_continue_and_snapshots_stay_fixed(self):
        batch = self.start(mode="batch", models=["mock-error", "mock-good"], repeats=3)
        self.finish(batch)
        rows = self.service.samples(self.owner)
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(r["status"] == "failed" for r in rows), 3)
        self.assertEqual(self.server.max_active, 1)
        self.assertNotIn(KEY, self.service.history.path.read_bytes().decode(errors="ignore"))
        for call in self.server.requests:
            self.assertEqual(call["body"]["input"], [{"role": "user", "content": catalog()[0]["prompt"]}])
        altered = catalog(); altered[0]["prompt"] = "new"
        self.assertEqual(self.service.samples(self.owner)[0]["question"]["prompt"], "Generate an SVG of a pelican riding a bicycle")

    def test_random_plan_default_and_frozen_samples_match_actual_requests(self):
        batch = self.service.start(self.owner, {"base_url": self.url, "api_key": KEY,
                                               "models": ["mock-json"], "mode": "once"})
        self.assertIs(batch["config"]["random_questions"], True)
        frozen = {q["id"]: deepcopy(q) for q in batch["config"]["questions"]}
        for q in batch["config"]["questions"]:
            q.update(prompt="changed after submission", title="changed")
        result = self.finish(batch)
        self.assertEqual(result["completed_requests"], 1)
        sample = self.service.samples(self.owner)[0]
        self.assertTrue(sample["random_questions"])
        self.assertEqual(sample["question"], frozen[sample["question"]["id"]])
        self.assertEqual(self.server.requests[0]["body"]["input"], [{"role": "user", "content": sample["question"]["prompt"]}])
        self.assertNotIn("random_questions", sample["parameters"])
        self.assertNotIn("questions", sample["parameters"])
        self.assertEqual(self.service.export(self.owner)["samples"][0]["question"], sample["question"])

    def test_random_scheduled_rounds_share_questions_and_continue_draw_bag(self):
        batch = self.start(random_questions=True, mode="scheduled", scope="batch", repeats=2,
                           models=["mock-good", "mock-json"], max_rounds=2, interval_seconds=0)
        result = self.finish(batch)
        self.assertEqual(result["completed_requests"], 8)
        rows = self.service.samples(self.owner)
        draws = []
        for round_number in (1, 2):
            for repetition in (1, 2):
                group = [r for r in rows if r["round"] == round_number and r["repetition"] == repetition]
                self.assertEqual(len(group), 2)
                self.assertEqual({r["requested_model"] for r in group}, {"mock-good", "mock-json"})
                self.assertEqual(group[0]["question"], group[1]["question"])
                draws.append(group[0]["question"]["id"])
        self.assertEqual(len(set(draws)), 4)
        self.assertEqual(self.server.max_active, 1)

    def test_continuous_multiple_rounds_ignores_schedule_and_stops_without_new_requests(self):
        batch = self.start(mode="continuous", interval_seconds=9999, max_rounds=1)
        wait_for(lambda: len(self.server.requests) >= 4)
        self.service.stop(self.owner, batch["id"])
        self.finish(batch)
        count = len(self.server.requests)
        time.sleep(.15)
        self.assertEqual(len(self.server.requests), count)
        self.assertGreaterEqual(count, 4)
        self.assertEqual(self.server.max_active, 1)

    def test_scheduled_first_round_immediate_and_bounded(self):
        batch = self.start(mode="scheduled", interval_seconds=1, max_rounds=2)
        self.finish(batch)
        self.assertEqual(len(self.server.requests), 2)
        self.assertGreaterEqual(self.server.requests[1]["started"] - self.server.requests[0]["started"], 1)

    def test_continuous_failure_keeps_every_failed_attempt_and_continues(self):
        batch = self.start(mode="continuous", models=["mock-error"])
        wait_for(lambda: len(self.server.requests) >= 3)
        self.service.stop(self.owner, batch["id"])
        self.finish(batch)
        rows = self.service.samples(self.owner)
        self.assertGreaterEqual(sum(r["status"] == "failed" for r in rows), 2)
        self.assertEqual(len(rows), self.service.tasks(self.owner)[0]["completed_requests"])
        self.assertFalse(any(r["reviews"] for r in rows))

    def test_batch_continuous_round_is_whole_cartesian_plan(self):
        batch = self.start(mode="continuous", scope="batch", models=["mock-good", "mock-json"], repeats=2)
        wait_for(lambda: len(self.server.requests) >= 8)
        self.service.stop(self.owner, batch["id"])
        self.finish(batch)
        rows = self.service.samples(self.owner)
        first = [r for r in rows if r["round"] == 1]
        self.assertEqual(len(first), 4)
        self.assertEqual({(r["requested_model"], r["repetition"]) for r in first}, {("mock-good", 1), ("mock-good", 2), ("mock-json", 1), ("mock-json", 2)})
        self.assertEqual(self.server.max_active, 1)

    def test_stop_cancels_wait_and_stale_callback_cannot_restart(self):
        batch = self.start(mode="scheduled", interval_seconds=1, max_rounds=0)
        wait_for(lambda: self.service.tasks(self.owner)[0]["status"] == "waiting")
        self.service.stop(self.owner, batch["id"])
        self.finish(batch)
        time.sleep(1.1)
        self.assertEqual(len(self.server.requests), 1)

    def test_stop_inflight_keeps_partial_and_never_launches_next(self):
        batch = self.start(mode="continuous", models=["mock-slow"], random_questions=True)
        wait_for(lambda: self.service.samples(self.owner) and self.service.samples(self.owner)[0].get("raw_output"))
        self.service.stop(self.owner, batch["id"])
        result = self.finish(batch)
        self.assertEqual(result["status"], "cancelled")
        rows = self.service.samples(self.owner)
        self.assertEqual(rows[0]["status"], "cancelled")
        self.assertTrue(rows[0]["raw_output"])
        self.assertEqual(len(self.server.requests), 1)

    def test_storage_failure_stops_before_any_more_requests(self):
        history = self.service.history
        original = history.put
        def fail(kind, owner, data):
            if kind == "sample" and data["status"] == "success": raise HistoryError("simulated disk full")
            original(kind, owner, data)
        with patch.object(history, "put", fail):
            batch = self.start(mode="continuous")
            finished = self.finish(batch)
            self.assertEqual(finished["status"], "storage_error")
            self.assertTrue(finished["unsaved_sample"])
            self.assertEqual(len(self.server.requests), 1)

    def test_restart_marks_journal_interrupted_and_never_reissues_requests(self):
        sample = self.ready_sample()
        sample["status"] = "running"; sample["raw_output"] = "<svg>partial"
        self.service.history.put("sample", self.owner, sample)
        batch = self.service.tasks(self.owner)[0]; batch["status"] = "running"
        self.service.history.put("batch", self.owner, batch)
        count = len(self.server.requests)
        fresh = PelicanService(self.temp.name)
        self.assertEqual(fresh.tasks(self.owner)[0]["status"], "interrupted")
        self.assertEqual(fresh.samples(self.owner)[0]["status"], "interrupted")
        self.assertEqual(len(self.server.requests), count)
        self.assertEqual(fresh.samples(self.owner)[0]["raw_output"], "<svg>partial")

    def test_two_reviews_disagreement_blind_and_recheck_preserve_revisions(self):
        sample = self.ready_sample()
        self.assertIsNone(review_summary(sample)["total"])
        first = self.service.add_review(self.owner, sample["id"], review(0))
        self.assertEqual(review_summary(first)["total"], 0)
        self.assertEqual(review_summary(first)["count"], 1)
        blind = self.service.blind(self.owner, "two", {})
        payload = json.dumps(blind)
        self.assertNotIn("mock-good", payload)
        self.assertNotIn("raw_output", payload)
        self.service.blind_review(self.owner, blind["id"], blind["samples"][0]["alias"], review(5))
        result = self.service.history.get("sample", self.owner, sample["id"])
        self.assertEqual(review_summary(result)["total"], 10)
        self.assertTrue(review_summary(result)["needs_recheck"])
        self.assertFalse(review_summary(result)["double_blind"])
        checked = self.service.recheck(self.owner, sample["id"], {"reviewer": "third", "note": "compared support contact"})
        self.assertFalse(review_summary(checked)["needs_recheck"])
        with self.assertRaises(ValueError): self.service.add_review(self.owner, sample["id"], review(3, "third"))
        updated = self.service.add_review(self.owner, sample["id"], review(2))
        self.assertEqual(len(updated["review_audit"]), 1)
        self.assertIsNone(updated["recheck"])

    def test_policy_upgrade_rechecks_history_once_without_regeneration_or_evidence_changes(self):
        self.finish(self.start(models=["mock-animated"]))
        blocked = self.service.samples(self.owner)[0]
        blocked["technical"].pop("checker_version")
        blocked["technical"].update(safe=False, issues=["禁止外部或动态样式", "禁止元素：animate"])
        self.service.history.put("sample", self.owner, blocked)
        scored = self.ready_sample()
        scored = self.service.add_review(self.owner, scored["id"], review(4))
        scored["technical"].pop("checker_version")
        self.service.history.put("sample", self.owner, scored)
        before = {r["id"]: r for r in self.service.samples(self.owner)}
        count = len(self.server.requests)
        for restart in range(2):
            fresh = PelicanService(self.temp.name)
            after = fresh.samples(self.owner)
            self.assertEqual(len(self.server.requests), count)
            for sample in after:
                old = before[sample["id"]]
                self.assertTrue(sample["technical"]["safe"])
                self.assertEqual(sample["technical"]["checker_version"], CHECKER_VERSION)
                self.assertEqual(len(sample["technical_audit"]), 1)
                self.assertEqual(sample["technical_audit"][0]["technical"], old["technical"])
                self.assertEqual({k: v for k, v in sample.items() if k not in {"technical", "technical_audit"}},
                                 {k: v for k, v in old.items() if k not in {"technical", "technical_audit"}})
            fresh.close()

    def test_reloading_animated_sample_cannot_replace_scoring_image(self):
        self.finish(self.start(models=["mock-animated"]))
        sample = self.service.samples(self.owner)[0]
        evidence = self.service.render(self.owner, sample["id"], {"success": True, "svg_sha256": svg_hash(sample["svg"]), "png": png_fixture()})
        repeated = self.service.render(self.owner, sample["id"], {"success": True, "svg_sha256": svg_hash(sample["svg"]), "png": "invalid-replacement"})
        self.assertEqual(evidence["render_evidence"], repeated["render_evidence"])
        self.assertEqual(evidence["render_evidence"]["capture_policy"], "first-loaded-frame-v1")

    def test_animation_reviews_require_observation_and_blind_review_receives_animation(self):
        animation_id = next(q["id"] for q in catalog() if q.get("animation"))
        self.finish(self.start(models=["mock-animated"], questions=[animation_id]))
        sample = self.service.samples(self.owner)[0]
        sample = self.service.render(self.owner, sample["id"], {"success": True, "svg_sha256": svg_hash(sample["svg"]), "png": png_fixture()})
        for confirmation in (None, False, 1, "true"):
            with self.subTest(confirmation=confirmation), self.assertRaises(ValueError):
                self.service.add_review(self.owner, sample["id"], review(3) | {"animation_observed": confirmation})
        first = self.service.add_review(self.owner, sample["id"], review(3) | {"animation_observed": True})
        evidence = first["reviews"][0]
        self.assertEqual(evidence["evidence_type"], "svg-animation")
        self.assertEqual(evidence["svg_sha256"], svg_hash(sample["svg"]))
        self.assertIs(evidence["animation_observed"], True)
        self.assertEqual(evidence["image_sha256"], sample["render_evidence"]["sha256"])
        blind = self.service.blind(self.owner, "two", {})
        anonymous = blind["samples"][0]
        self.assertEqual(set(anonymous), {"alias", "image", "question"})
        self.assertTrue(anonymous["image"].startswith("data:image/svg+xml;base64,"))
        self.assertEqual(base64.b64decode(anonymous["image"].split(",", 1)[1]).decode(), sample["svg"])
        self.assertNotIn("mock-animated", json.dumps(blind))
        self.assertNotIn(sample["id"], json.dumps(blind))
        with self.assertRaises(ValueError):
            self.service.blind_review(self.owner, blind["id"], anonymous["alias"], review(4))
        self.service.blind_review(self.owner, blind["id"], anonymous["alias"], review(4) | {"animation_observed": True})
        saved = self.service.history.get("sample", self.owner, sample["id"])
        self.assertTrue(saved["reviews"][1]["blind"])
        self.assertTrue(saved["reviews"][1]["animation_observed"])

    def test_animation_ai_review_rejects_static_frame_before_upstream_request(self):
        self.finish(self.start(models=["mock-animated"], random_questions=True))
        sample = self.service.samples(self.owner)[0]
        self.service.render(self.owner, sample["id"], {"success": True, "svg_sha256": svg_hash(sample["svg"]), "png": png_fixture()})
        requests_before = len(self.server.requests)
        with self.assertRaisesRegex(ValueError, "单帧"):
            asyncio.run(self.service.ai_review(self.owner, sample["id"], {"base_url": self.url, "api_key": KEY,
                       "model": "mock-judge", "enabled": True, "vision_confirmed": True}))
        self.assertEqual(len(self.server.requests), requests_before)
        self.assertEqual(self.service.history.all("ai_attempt", self.owner), [])
        self.assertFalse(self.service.ai_running)

    def test_invalid_and_cancelled_outputs_cannot_be_scored(self):
        self.finish(self.start(models=["mock-unsafe"]))
        r = self.service.samples(self.owner)[0]
        with self.assertRaises(ValueError): self.service.add_review(self.owner, r["id"], review(5))
        with self.assertRaises(ValueError): self.service.render(self.owner, r["id"], {"success": True, "svg_sha256": svg_hash(r["svg"]), "png": png_fixture()})

    def test_two_separate_blind_sessions_are_recorded_as_two_human_blind_reviews(self):
        sample = self.ready_sample()
        for name in ("alice", "bob"):
            session = self.service.blind(self.owner, name, {})
            self.service.blind_review(self.owner, session["id"], session["samples"][0]["alias"], review(4))
        result = self.service.history.get("sample", self.owner, sample["id"])
        self.assertTrue(review_summary(result)["double_blind"])
        self.assertEqual(review_summary(result)["human_count"], 2)

    def test_missing_ai_image_never_fabricates_score_or_sends_request(self):
        self.finish(self.start())
        sample = self.service.samples(self.owner)[0]
        before = len(self.server.requests)
        with self.assertRaises(ValueError):
            asyncio.run(self.service.ai_review(self.owner, sample["id"], {"base_url": self.url, "api_key": KEY, "model": "mock-judge", "enabled": True, "vision_confirmed": True}))
        self.assertEqual(len(self.server.requests), before)
        self.assertIsNone(review_summary(sample)["total"])

    def test_optional_ai_uses_image_and_records_configuration_no_svg_or_test_model(self):
        sample = self.ready_sample()
        config = {"base_url": self.url, "api_key": KEY, "model": "mock-judge", "enabled": True, "vision_confirmed": True}
        result = asyncio.run(self.service.ai_review(self.owner, sample["id"], config))
        self.assertEqual(result["reviews"][0]["method"], "ai")
        sent = self.server.requests[-1]["body"]
        self.assertEqual(sent["input"][0]["content"][1]["type"], "input_image")
        self.assertNotIn("<svg", json.dumps(sent))
        self.assertNotIn("mock-good", json.dumps(sent))
        self.assertTrue(result["reviews"][0]["metadata"]["parameters"])
        with self.assertRaises(ValueError): asyncio.run(self.service.ai_review(self.owner, sample["id"], config | {"model": "mock-bad-judge"}))
        self.assertEqual(len(self.service.history.get("sample", self.owner, sample["id"])["reviews"]), 1)
        self.assertEqual(len(self.service.history.all("ai_attempt", self.owner)), 2)
        with self.assertRaises(ValueError): asyncio.run(self.service.ai_review(self.owner, sample["id"], config | {"enabled": False}))

    def test_reports_denominators_performance_separate_and_formula_safe_csv(self):
        sample = self.ready_sample()
        self.service.add_review(self.owner, sample["id"], review(4))
        self.finish(self.start())
        data = self.service.export(self.owner)
        group = data["groups"][0]
        self.assertEqual((group["samples"], group["scored"], group["total_mean"]), (2, 1, 16))
        self.assertEqual(group["scoring_coverage"], .5)
        self.assertEqual(group["token_usage"]["input"]["sum"], 0)
        self.assertIn("不是官方基准分", html_export(data))
        self.assertIn("data:image/png;base64", html_export(data))
        data["samples"][0]["requested_model"] = "=HYPERLINK(\"bad\")"
        rows = list(csv.reader(io.StringIO(csv_export(data))))
        self.assertTrue(rows[1][3].startswith("'="))
        self.assertNotIn(KEY, json.dumps(data))

    def test_filters_and_history_owner_isolation(self):
        self.finish(self.start(mode="batch", models=["mock-good", "mock-missing"], repeats=1))
        self.assertEqual(len(self.service.samples(self.owner, {"technical": "missing"})), 1)
        self.assertEqual(len(self.service.samples(self.owner, {"scoring": "pending"})), 2)
        self.assertEqual(len(self.service.samples(self.owner, {"model": "mock-good", "mode": "batch"})), 1)
        self.assertEqual(self.service.samples("someone-else"), [])
        with self.assertRaises(KeyError): self.service.history.get("sample", "someone-else", self.service.samples(self.owner)[0]["id"])

    def test_database_pagination_and_filters_match_full_history(self):
        self.finish(self.start(mode="batch", models=["mock-good", "mock-missing"], repeats=7))
        page, count = self.service.history.sample_page(self.owner, {}, 1)
        self.assertEqual((len(page), count), (12, 14))
        rest, _ = self.service.history.sample_page(self.owner, {}, 2)
        self.assertEqual(len(rest), 2)
        self.assertFalse({r["id"] for r in page} & {r["id"] for r in rest})
        for filters in [{"model": "mock-good"}, {"technical": "missing"}, {"scoring": "pending"}, {"question": "pelican-bicycle", "mode": "batch"}, {"from": "2000-01-01T00:00:00Z", "to": "2100-01-01T00:00:00Z"}]:
            rows, count = self.service.history.sample_page(self.owner, filters, 1)
            expected = self.service.samples(self.owner, filters)
            self.assertEqual(count, len(expected))
            self.assertEqual([r["id"] for r in rows], [r["id"] for r in expected[:12]])


class PelicanWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = mock_provider()
        self.server, self.url = self.provider.__enter__()
        self.app = create_app(pelican_dir=self.temp.name)
        self.client = self.app.test_client()
        self.headers = browser_headers(self.app, self.client)
        self.client.get('/pelican')

    def tearDown(self):
        self.app.extensions["pelican"].close()
        self.provider.__exit__(None, None, None)
        self.temp.cleanup()

    def test_navigation_access_guards_and_models(self):
        for route in ('/', '/converter', '/network', '/pelican'):
            self.assertIn('href="/pelican"', self.client.get(route).text)
        self.assertEqual(self.client.get('/api/pelican/tasks').status_code, 403)
        self.assertEqual(self.client.get('/pelican', headers={"Origin": "https://evil.test"}).status_code, 403)
        response = self.client.post('/api/pelican/models', json={"base_url": self.url, "api_key": KEY}, headers=self.headers)
        self.assertIn('mock-good', response.json['models'])

    def test_refresh_cross_page_downloads_redaction_and_restart_cookie(self):
        response = self.client.post('/api/pelican/tasks', json={"base_url": self.url, "api_key": KEY, "models": ["mock-echo"]}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        self.client.get('/converter')
        wait_for(lambda: self.client.get('/api/pelican/tasks', headers=self.headers).json['tasks'][0]['status'] == 'completed')
        sample = self.client.get('/api/pelican/samples', headers=self.headers).json['samples'][0]
        self.assertTrue(sample['redacted'])
        for format_name in ['json', 'csv', 'html']:
            response = self.client.get('/api/pelican/export/' + format_name, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertIn('attachment;', response.headers['Content-Disposition'])
            self.assertNotIn(KEY, response.text)
        svg = self.client.get(f'/api/pelican/samples/{sample["id"]}/svg', headers=self.headers)
        self.assertEqual(svg.text, SVG)
        self.assertEqual(svg.mimetype, 'application/octet-stream')
        other = self.app.test_client(); other_headers = browser_headers(self.app, other)
        other.get('/pelican')
        self.assertEqual(other.get('/api/pelican/samples', headers=other_headers).json['total'], 0)
        self.assertEqual(other.get(f'/api/pelican/samples/{sample["id"]}', headers=other_headers).status_code, 404)
        cookie = self.client.get_cookie(COOKIE)
        fresh = create_app(pelican_dir=self.temp.name)
        new = fresh.test_client(); new.set_cookie(COOKIE, cookie.value)
        headers = browser_headers(fresh, new)
        self.assertEqual(new.get('/api/pelican/samples', headers=headers).json['total'], 1)
        fresh.extensions['pelican'].close()


if __name__ == '__main__':
    unittest.main()
